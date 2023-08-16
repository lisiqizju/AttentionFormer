import time
import torch
import torch.nn as nn
import numpy as np
import math
from torch.nn.functional import interpolate


def decor_time(func):
    def func2(*args, **kw):
        now = time.time()
        y = func(*args, **kw)
        t = time.time() - now
        print('call <{}>, time={}'.format(func.__name__, t))
        return y
    return func2


class AutoCorrelation(nn.Module):
    """
    AutoCorrelation Mechanism with the following two phases:
    (1) period-based dependencies discovery
    (2) time delay aggregation
    This block can replace the self-attention family mechanism seamlessly.
    """
    def __init__(self, mask_flag=True, factor=1, scale=None, attention_dropout=0.1, output_attention=False, configs=None):
        super(AutoCorrelation, self).__init__()
        print('Autocorrelation used !')
        self.factor = factor
        self.scale = scale
        self.mask_flag = mask_flag
        self.output_attention = output_attention
        self.dropout = nn.Dropout(attention_dropout)
        self.agg = None
        self.use_wavelet = configs.wavelet

    # @decor_time
    def time_delay_agg_training(self, values, corr):
        """
        SpeedUp version of Autocorrelation (a batch-normalization style design)
        This is for the training phase.
        """
        head = values.shape[1]
        channel = values.shape[2]
        length = values.shape[3]
        # find top k
        top_k = int(self.factor * math.log(length))
        mean_value = torch.mean(torch.mean(corr, dim=1), dim=1)
        index = torch.topk(torch.mean(mean_value, dim=0), top_k, dim=-1)[1]
        weights = torch.stack([mean_value[:, index[i]] for i in range(top_k)], dim=-1)
        # update corr
        tmp_corr = torch.softmax(weights, dim=-1)
        # aggregation
        tmp_values = values
        delays_agg = torch.zeros_like(values).float()
        for i in range(top_k):
            pattern = torch.roll(tmp_values, -int(index[i]), -1)
            delays_agg = delays_agg + pattern * \
                         (tmp_corr[:, i].unsqueeze(1).unsqueeze(1).unsqueeze(1).repeat(1, head, channel, length))
        return delays_agg  # size=[B, H, d, S]

    def time_delay_agg_inference(self, values, corr):
        """
        SpeedUp version of Autocorrelation (a batch-normalization style design)
        This is for the inference phase.
        """
        batch = values.shape[0]
        head = values.shape[1]
        channel = values.shape[2]
        length = values.shape[3]
        # index init
        init_index = torch.arange(length).unsqueeze(0).unsqueeze(0).unsqueeze(0).repeat(batch, head, channel, 1).cuda()
        # find top k
        top_k = int(self.factor * math.log(length))
        mean_value = torch.mean(torch.mean(corr, dim=1), dim=1)
        weights = torch.topk(mean_value, top_k, dim=-1)[0]
        delay = torch.topk(mean_value, top_k, dim=-1)[1]
        # update corr
        tmp_corr = torch.softmax(weights, dim=-1)
        # aggregation
        tmp_values = values.repeat(1, 1, 1, 2)
        delays_agg = torch.zeros_like(values).float()
        for i in range(top_k):
            tmp_delay = init_index + delay[:, i].unsqueeze(1).unsqueeze(1).unsqueeze(1).repeat(1, head, channel, length)
            pattern = torch.gather(tmp_values, dim=-1, index=tmp_delay)
            delays_agg = delays_agg + pattern * \
                         (tmp_corr[:, i].unsqueeze(1).unsqueeze(1).unsqueeze(1).repeat(1, head, channel, length))
        return delays_agg

    def time_delay_agg_full(self, values, corr):
        """
        Standard version of Autocorrelation
        """
        batch = values.shape[0]
        head = values.shape[1]
        channel = values.shape[2]
        length = values.shape[3]
        # index init
        init_index = torch.arange(length).unsqueeze(0).unsqueeze(0).unsqueeze(0).repeat(batch, head, channel, 1).cuda()
        # find top k
        top_k = int(self.factor * math.log(length))
        weights = torch.topk(corr, top_k, dim=-1)[0]
        delay = torch.topk(corr, top_k, dim=-1)[1]
        # update corr
        tmp_corr = torch.softmax(weights, dim=-1)
        # aggregation
        tmp_values = values.repeat(1, 1, 1, 2)
        delays_agg = torch.zeros_like(values).float()
        for i in range(top_k):
            tmp_delay = init_index + delay[..., i].unsqueeze(-1)
            pattern = torch.gather(tmp_values, dim=-1, index=tmp_delay)
            delays_agg = delays_agg + pattern * (tmp_corr[..., i].unsqueeze(-1))
        return delays_agg

    def forward(self, queries, keys, values, attn_mask):
        B, L, H, E = queries.shape
        _, S, _, D = values.shape
        if L > S:
            zeros = torch.zeros_like(queries[:, :(L - S), :]).float()
            values = torch.cat([values, zeros], dim=1)
            keys = torch.cat([keys, zeros], dim=1)
        else:
            values = values[:, :L, :, :]
            keys = keys[:, :L, :, :]

        # period-based dependencies
        if self.use_wavelet != 2:
            if self.use_wavelet == 1:
                j_list = self.j_list
                queries = queries.reshape([B, L, -1])
                keys = keys.reshape([B, L, -1])
                Ql, Qh_list = self.dwt1d(queries.transpose(1, 2))  # [B, H*D, L]
                Kl, Kh_list = self.dwt1d(keys.transpose(1, 2))
                qs = [queries.transpose(1, 2)] + Qh_list + [Ql]  # [B, H*D, L]
                ks = [keys.transpose(1, 2)] + Kh_list + [Kl]
                q_list = []
                k_list = []
                for q, k, j in zip(qs, ks, j_list):
                    q_list += [interpolate(q, scale_factor=j, mode='linear')[:, :, -L:]]
                    k_list += [interpolate(k, scale_factor=j, mode='linear')[:, :, -L:]]
                queries = torch.stack([i.reshape([B, H, E, L]) for i in q_list], dim=3).reshape([B, H, -1, L]).permute(0, 3, 1, 2)
                keys = torch.stack([i.reshape([B, H, E, L]) for i in k_list], dim=3).reshape([B, H, -1, L]).permute(0, 3, 1, 2)
            else:
                pass
            q_fft = torch.fft.rfft(queries.permute(0, 2, 3, 1).contiguous(), dim=-1)  # size=[B, H, E, L]
            k_fft = torch.fft.rfft(keys.permute(0, 2, 3, 1).contiguous(), dim=-1)
            res = q_fft * torch.conj(k_fft)
            corr = torch.fft.irfft(res, dim=-1) # size=[B, H, E, L]

            # time delay agg
            if self.training:
                V = self.time_delay_agg_training(values.permute(0, 2, 3, 1).contiguous(), corr).permute(0, 3, 1, 2)  # [B, L, H, E], [B, H, E, L] -> [B, L, H, E]
            else:
                V = self.time_delay_agg_inference(values.permute(0, 2, 3, 1).contiguous(), corr).permute(0, 3, 1, 2)
        else:
            V_list = []
            queries = queries.reshape([B, L, -1])
            keys = keys.reshape([B, L, -1])
            values = values.reshape([B, L, -1])
            Ql, Qh_list = self.dwt1d(queries.transpose(1, 2))  # [B, H*D, L]
            Kl, Kh_list = self.dwt1d(keys.transpose(1, 2))
            Vl, Vh_list = self.dwt1d(values.transpose(1, 2))
            qs = Qh_list + [Ql]  # [B, H*D, L]
            ks = Kh_list + [Kl]
            vs = Vh_list + [Vl]
            for q, k, v in zip(qs, ks, vs):
                q = q.reshape([B, H, E, -1])
                k = k.reshape([B, H, E, -1])
                v = v.reshape([B, H, E, -1]).permute(0, 3, 1, 2)
                q_fft = torch.fft.rfft(q.contiguous(), dim=-1)
                k_fft = torch.fft.rfft(k.contiguous(), dim=-1)
                res = q_fft * torch.conj(k_fft)
                corr = torch.fft.irfft(res, dim=-1)  # [B, H, E, L]
                if self.training:
                    V = self.time_delay_agg_training(v.permute(0, 2, 3, 1).contiguous(), corr).permute(0, 3, 1, 2)
                else:
                    V = self.time_delay_agg_inference(v.permute(0, 2, 3, 1).contiguous(), corr).permute(0, 3, 1, 2)
                V_list += [V]
            Vl = V_list[-1].reshape([B, -1, H*E]).transpose(1, 2)
            Vh_list = [i.reshape([B, -1, H*E]).transpose(1, 2) for i in V_list[:-1]]
            V = self.dwt1div((Vl, Vh_list)).reshape([B, H, E, -1]).permute(0, 3, 1, 2)
            # corr = self.dwt1div((V_list[-1], V_list[:-1]))

        if self.output_attention:
            return (V.contiguous(), corr.permute(0, 3, 1, 2))  # size = [B, L, H, E]
        else:
            return (V.contiguous(), None)


class AutoCorrelationLayer(nn.Module):
    def __init__(self, correlation, d_model, n_heads, d_keys=None,
                 d_values=None):
        super(AutoCorrelationLayer, self).__init__()

        d_keys = d_keys or (d_model // n_heads)
        d_values = d_values or (d_model // n_heads)
        # d_keys = d_model 
        # d_values = d_model 

        self.inner_correlation = correlation
        self.query_projection = nn.Linear(d_model, d_keys * n_heads)
        self.key_projection = nn.Linear(d_model, d_keys * n_heads)
        self.value_projection = nn.Linear(d_model, d_values * n_heads)
        self.out_projection = nn.Linear(d_values * n_heads, d_model)
        self.n_heads = n_heads

    def forward(self, queries, keys, values, attn_mask):
        B, L, _ = queries.shape
        _, S, _ = keys.shape
        H = self.n_heads

        queries = self.query_projection(queries).view(B, L, H, -1)
        keys = self.key_projection(keys).view(B, S, H, -1)
        values = self.value_projection(values).view(B, S, H, -1)

        out, attn = self.inner_correlation(
            queries,
            keys,
            values,
            attn_mask
        )
        out = out.contiguous().view(B, L, -1)
        return self.out_projection(out), attn
    

class RAttentionLayer(nn.Module):
    def __init__(self, correlation, d_model, n_heads,d_keys=None, 
                 d_values=None):
        super(RAttentionLayer, self).__init__()
        self.segmented_v =16
        self.segmented_ratio =0.5

        self.attention_layer=AutoCorrelationLayer(correlation, d_model, n_heads, 
                 d_keys=None, d_values=None)


    def forward(self, queries, keys, values, attn_mask):
        self.device=queries.device

        c_out=torch.zeros(queries.size()).to(self.device)
        segmented_q=int(self.segmented_ratio*self.segmented_v)
        queries_subseq_len=queries.size()[1]//segmented_q
        keys_subseq_len=values_sebseq_len=keys.size()[1]//self.segmented_v

        # for i in range(self.n_R):
        #     queries_subseq=queries[:,queries_sebseq_len*i:queries_sebseq_len*(i+1),:]

        #     keys_subseq=keys[:,keys_sebseq_len*i:keys_sebseq_len*(i+1),:]
        #     values_subseq=values[:,values_sebseq_len*i:values_sebseq_len*(i+1),:]

        #     h,attn=self.attention_layer(queries_subseq, keys_subseq, values_subseq, attn_mask)
        #     # h,attn=self.attention_layer(queries, keys_subseq, values_subseq, attn_mask)
        #     # h,attn=self.attention_layer(queries_subseq, keys, values, attn_mask)
        #     c_out[:,queries_sebseq_len*i:queries_sebseq_len*(i+1)]=h
        #     # c_out+=h
          
        # if self.segmented_ratio < 1:
        #     self.segmented_q = self.segmented_v * self.segmented_v
        #     for i in range(self.segmented_v):
        #         queries_subseq=queries[:,queries_subseq_len*i:queries_subseq_len*(i+1),:]
        #         keys_subseq=keys[:,keys_subseq_len*i:keys_subseq_len*(i+1),:]
        #         values_subseq=values[:,values_sebseq_len*i:values_sebseq_len*(i+1),:]
        #         for j in range(self.segmented_ratio):
        #             queries_subsubseq_len=queries_subseq_len//self.segmented_ratio
        #             queries_subsubseq=queries_subseq[:,queries_subsubseq_len*j:queries_subsubseq_len*(j+1),:]
        #             h,attn=self.attention_layer(queries_subsubseq, keys_subseq, values_subseq, attn_mask)
        #             c_out[:,queries_subsubseq_len*i:queries_subsubseq_len*(i+1)]+=h
        # else:
        #     for i in range(self.segmented_v):
        #         queries_subseq=queries[:,queries_subseq_len*i:queries_subseq_len*(i+1),:]
        #         keys_subseq=keys[:,keys_subseq_len*i:keys_subseq_len*(i+1),:]
        #         values_subseq=values[:,values_sebseq_len*i:values_sebseq_len*(i+1),:]
        #         for j in range(self.segmented_ratio):
        #             queries_subsubseq_len=queries_subseq_len//self.segmented_ratio
        #             queries_subsubseq=queries_subseq[:,queries_subsubseq_len*j:queries_subsubseq_len*(j+1),:]
        #             h,attn=self.attention_layer(queries_subsubseq, keys_subseq, values_subseq, attn_mask)
        #             c_out[:,queries_subsubseq_len*i:queries_subsubseq_len*(i+1)]+=h
        
        for i in range(segmented_q):
            queries_subseq=queries[:,queries_subseq_len*i:queries_subseq_len*(i+1),:]
            for j in range(self.segmented_v):
                keys_subseq=keys[:,keys_subseq_len*j:keys_subseq_len*(j+1),:]
                values_subseq=values[:,values_sebseq_len*j:values_sebseq_len*(j+1),:]
                h,attn=self.attention_layer(queries_subseq, keys_subseq, values_subseq, attn_mask)
                c_out[:,queries_subseq_len*i:queries_subseq_len*(i+1)]+=h

        # for i in range(self.segmented_v):
        #         queries_subseq=queries[:,queries_subseq_len*i:queries_subseq_len*(i+1),:]
        #         keys_subseq=keys[:,keys_subseq_len*i:keys_subseq_len*(i+1),:]
        #         values_subseq=values[:,values_sebseq_len*i:values_sebseq_len*(i+1),:]
        #         for j in range(segmented_q):
        #             queries_subsubseq_len=queries_subseq_len//self.segmented_ratio
        #             queries_subsubseq=queries_subseq[:,queries_subsubseq_len*j:queries_subsubseq_len*(j+1),:]
        #             h,attn=self.attention_layer(queries_subsubseq, keys_subseq, values_subseq, attn_mask)
        #             c_out[:,queries_subsubseq_len*j:queries_subsubseq_len*(j+1)]+=h
        

        
        out=c_out  

        return out, attn