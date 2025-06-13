import itertools
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.checkpoint as checkpoint
from timm.models.layers import DropPath as TimmDropPath, to_2tuple, trunc_normal_
from typing import Tuple


class Conv2d_BN(torch.nn.Sequential):
    def __init__(self, a, b, ks=1, stride=1, pad=0, dilation=1,
                 groups=1, bn_weight_init=1):
        super().__init__()
        self.add_module('c', torch.nn.Conv2d(
            a, b, ks, stride, pad, dilation, groups, bias=False))
        bn = torch.nn.BatchNorm2d(b)
        torch.nn.init.constant_(bn.weight, bn_weight_init)
        torch.nn.init.constant_(bn.bias, 0)
        self.add_module('bn', bn)

    @torch.no_grad()
    def fuse(self):
        c, bn = self._modules.values()
        w = bn.weight / (bn.running_var + bn.eps)**0.5
        w = c.weight * w[:, None, None, None]
        b = bn.bias - bn.running_mean * bn.weight / \
            (bn.running_var + bn.eps)**0.5
        m = torch.nn.Conv2d(w.size(1) * self.c.groups, w.size(
            0), w.shape[2:], stride=self.c.stride, padding=self.c.padding, dilation=self.c.dilation, groups=self.c.groups)
        m.weight.data.copy_(w)
        m.bias.data.copy_(b)

        return m


class DropPath(TimmDropPath):
    def __init__(self, drop_prob=None):
        super().__init__(drop_prob=drop_prob)
        self.drop_prob = drop_prob

    def __repr__(self):
        msg = super().__repr__()
        msg += f'(drop_prob={self.drop_prob})'

        return msg


class PatchEmbed(nn.Module):
    def __init__(self, in_chans, embed_dim, resolution, activation):
        super().__init__()
        img_size: Tuple[int, int] = to_2tuple(resolution)
        self.patches_resolution = (img_size[0] // 4, img_size[1] // 4)
        self.num_patches = self.patches_resolution[0] * \
            self.patches_resolution[1]
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        n = embed_dim
        self.seq = nn.Sequential(
            Conv2d_BN(in_chans, n // 2, 3, 2, 1),
            activation(),
            Conv2d_BN(n // 2, n, 3, 2, 1),
        )

    def forward(self, x):
        return self.seq(x)


class MBConv(nn.Module):
    def __init__(self, in_chans, out_chans, expand_ratio,
                 activation, drop_path):
        super().__init__()
        self.in_chans = in_chans
        self.hidden_chans = int(in_chans * expand_ratio)
        self.out_chans = out_chans

        self.conv1 = Conv2d_BN(in_chans, self.hidden_chans, ks=1)
        self.act1 = activation()

        self.conv2 = Conv2d_BN(self.hidden_chans, self.hidden_chans,
                               ks=3, stride=1, pad=1, groups=self.hidden_chans)
        self.act2 = activation()

        self.conv3 = Conv2d_BN(
            self.hidden_chans, out_chans, ks=1, bn_weight_init=0.0)
        self.act3 = activation()

        self.drop_path = DropPath(
            drop_path) if drop_path > 0. else nn.Identity()

    def forward(self, x):
        shortcut = x

        x = self.conv1(x); x = self.act1(x)
        x = self.conv2(x); x = self.act2(x)
        x = self.conv3(x)
        x = self.drop_path(x)
        x += shortcut
        x = self.act3(x)

        return x


class PatchMerging(nn.Module):
    def __init__(self, input_resolution, dim, out_dim, activation):
        super().__init__()
        self.input_resolution = input_resolution
        self.dim = dim
        self.out_dim = out_dim
        self.act = activation()
        self.conv1 = Conv2d_BN(dim, out_dim, 1, 1, 0)

        stride_c = 2
        if out_dim in (320, 448, 576):
            stride_c = 1

        self.conv2 = Conv2d_BN(out_dim, out_dim, 3, stride_c, 1, groups=out_dim)
        self.conv3 = Conv2d_BN(out_dim, out_dim, 1, 1, 0)

    def forward(self, x):
        if x.ndim == 3:
            H, W = self.input_resolution
            B = len(x)
            x = x.view(B, H, W, -1).permute(0, 3, 1, 2)

        x = self.conv1(x); x = self.act(x)
        x = self.conv2(x); x = self.act(x)
        x = self.conv3(x)
        x = x.flatten(2).transpose(1, 2)

        return x


class ConvLayer(nn.Module):
    def __init__(self, dim, input_resolution, depth,
                 activation, drop_path=0., downsample=None,
                 use_checkpoint=False, out_dim=None, conv_expand_ratio=4.):
        super().__init__()
        self.dim = dim; self.input_resolution = input_resolution
        self.depth = depth; self.use_checkpoint = use_checkpoint

        self.blocks = nn.ModuleList([
            MBConv(dim, dim, conv_expand_ratio, activation,
                   drop_path[i] if isinstance(drop_path, list) else drop_path)
            for i in range(depth)])

        self.downsample = downsample(input_resolution, dim=dim,
                                     out_dim=out_dim, activation=activation) \
                          if downsample else None

    def forward(self, x):
        for blk in self.blocks:
            x = checkpoint.checkpoint(blk, x) if self.use_checkpoint else blk(x)
        if self.downsample is not None:
            x = self.downsample(x)

        return x


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None,
                 out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.norm = nn.LayerNorm(in_features)
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.act = act_layer()
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.norm(x)
        x = self.fc1(x); x = self.act(x); x = self.drop(x)
        x = self.fc2(x); x = self.drop(x)

        return x


class Attention(nn.Module):
    def __init__(self, dim, key_dim, num_heads=8, attn_ratio=4,
                 resolution=(14, 14)):
        super().__init__()
        assert len(resolution) == 2
        self.num_heads = num_heads
        self.scale = key_dim ** -0.5
        self.key_dim = key_dim
        self.nh_kd = key_dim * num_heads
        self.d = int(attn_ratio * key_dim)
        self.dh = self.d * num_heads
        self.attn_ratio = attn_ratio

        self.norm = nn.LayerNorm(dim)
        self.qkv = nn.Linear(dim, self.dh + self.nh_kd * 2)
        self.proj = nn.Linear(self.dh, dim)

        points = list(itertools.product(range(resolution[0]), range(resolution[1])))
        N = len(points)
        attention_offsets = {}
        idxs = []
        for p1 in points:
            for p2 in points:
                offset = (abs(p1[0] - p2[0]), abs(p1[1] - p2[1]))
                attention_offsets.setdefault(offset, len(attention_offsets))
                idxs.append(attention_offsets[offset])

        self.attention_biases = nn.Parameter(torch.zeros(num_heads, len(attention_offsets)))
        self.register_buffer('attention_bias_idxs',
                             torch.LongTensor(idxs).view(N, N), persistent=False)

    @torch.no_grad()
    def train(self, mode=True):
        super().train(mode)
        if mode and hasattr(self, 'ab'):
            del self.ab
        else:
            self.ab = self.attention_biases[:, self.attention_bias_idxs]

    def forward(self, x):
        B, N, C = x.shape
        x = self.norm(x)
        qkv = self.qkv(x)

        q, k, v = qkv.view(B, N, self.num_heads, -1).split(
            [self.key_dim, self.key_dim, self.d], dim=3)
        q, k, v = [t.permute(0, 2, 1, 3) for t in (q, k, v)]
        attn = (q @ k.transpose(-2, -1)) * self.scale \
               + (self.attention_biases[:, self.attention_bias_idxs] if self.training else self.ab)
        attn = attn.softmax(dim=-1)
        x = (attn @ v).transpose(1, 2).reshape(B, N, self.dh)
        x = self.proj(x)

        return x


class TinyViTBlock(nn.Module):
    def __init__(self, dim, input_resolution, num_heads, window_size=7,
                 mlp_ratio=4., drop=0., drop_path=0., local_conv_size=3,
                 activation=nn.GELU):
        super().__init__()
        self.dim = dim; self.input_resolution = input_resolution
        self.num_heads = num_heads; self.window_size = window_size
        self.mlp_ratio = mlp_ratio
        self.drop_path = DropPath(drop_path) if drop_path > 0 else nn.Identity()
        head_dim = dim // num_heads
        self.attn = Attention(dim, head_dim, num_heads,
                              attn_ratio=1,
                              resolution=(window_size, window_size))
        self.mlp = Mlp(in_features=dim, hidden_features=int(dim*mlp_ratio),
                       act_layer=activation, drop=drop)
        pad = local_conv_size // 2
        self.local_conv = Conv2d_BN(dim, dim, ks=local_conv_size,
                                    stride=1, pad=pad, groups=dim)

    def forward(self, x):
        B, L, C = x.shape
        H, W = self.input_resolution
        assert L == H*W
        res = x
        if H == self.window_size and W == self.window_size:
            x = self.attn(x)
        else:
            # pad, window-partition, attend, unpartition
            x = x.view(B, H, W, C)
            pad_b = (self.window_size - H % self.window_size) % self.window_size
            pad_r = (self.window_size - W % self.window_size) % self.window_size
            if pad_b or pad_r:
                x = F.pad(x, (0,0,0,pad_r,0,pad_b))
            pH, pW = H+pad_b, W+pad_r
            nH, nW = pH//self.window_size, pW//self.window_size
            x = x.view(B, nH, self.window_size, nW, self.window_size, C)\
                 .transpose(2,3)\
                 .reshape(B*nH*nW, self.window_size*self.window_size, C)
            x = self.attn(x)
            x = x.view(B, nH, nW, self.window_size, self.window_size, C)\
                 .transpose(2,3)\
                 .reshape(B, pH, pW, C)
            if pad_b or pad_r:
                x = x[:, :H, :W].contiguous()  
            x = x.view(B, L, C)

        x = res + self.drop_path(x)
        x = x.transpose(1,2).reshape(B, C, H, W)
        x = self.local_conv(x)
        x = x.view(B, C, L).transpose(1,2)
        x = x + self.drop_path(self.mlp(x))

        return x


class BasicLayer(nn.Module):
    def __init__(self, dim, input_resolution, depth, num_heads,
                 window_size, mlp_ratio=4., drop=0., drop_path=0.,
                 downsample=None, use_checkpoint=False,
                 local_conv_size=3, activation=nn.GELU, out_dim=None):
        super().__init__()
        self.blocks = nn.ModuleList([
            TinyViTBlock(dim=dim, input_resolution=input_resolution,
                         num_heads=num_heads, window_size=window_size,
                         mlp_ratio=mlp_ratio, drop=drop,
                         drop_path=drop_path[i] if isinstance(drop_path,list) else drop_path,
                         local_conv_size=local_conv_size,
                         activation=activation)
            for i in range(depth)])
        self.downsample = downsample(input_resolution, dim=dim,
                                     out_dim=out_dim, activation=activation) \
                          if downsample else None
        self.use_checkpoint = use_checkpoint

    def forward(self, x):
        for blk in self.blocks:
            x = checkpoint.checkpoint(blk, x) if self.use_checkpoint else blk(x)
        if self.downsample is not None:
            x = self.downsample(x)

        return x


class LayerNorm2d(nn.Module):
    def __init__(self, num_channels: int, eps: float = 1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(num_channels))
        self.bias = nn.Parameter(torch.zeros(num_channels))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        u = x.mean(1, keepdim=True)
        s = (x - u).pow(2).mean(1, keepdim=True)
        x = (x - u) / torch.sqrt(s + self.eps)
        x = self.weight[:, None, None] * x + self.bias[:, None, None]

        return x


class TinyViT(nn.Module):
    def __init__(self, img_size=224, in_chans=3, num_classes=1000,
                 embed_dims=[96,192,384,768], depths=[2,2,6,2],
                 num_heads=[3,6,12,24], window_sizes=[7,7,14,7],
                 mlp_ratio=4., drop_rate=0., drop_path_rate=0.1,
                 use_checkpoint=False, mbconv_expand_ratio=4.0,
                 local_conv_size=3, layer_lr_decay=1.0):
        super().__init__()
        self.img_size = img_size
        self.num_classes = num_classes
        self.depths = depths
        self.num_layers = len(depths)
        self.mlp_ratio = mlp_ratio
        activation = nn.GELU

        # Patch Embedding
        self.patch_embed = PatchEmbed(in_chans, embed_dims[0], img_size, activation)
        self.patches_resolution = self.patch_embed.patches_resolution

        # Stochastic Depth Schedule
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        # Build Stages
        self.layers = nn.ModuleList()
        for i_layer in range(self.num_layers):
            is_conv = (i_layer == 0)
            kwargs = dict(dim=embed_dims[i_layer],
                          input_resolution=(self.patches_resolution[0]//(2**(i_layer-1 if i_layer==3 else i_layer)),
                                            self.patches_resolution[1]//(2**(i_layer-1 if i_layer==3 else i_layer))),
                          depth=depths[i_layer],
                          drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer+1])],
                          downsample=PatchMerging if i_layer < self.num_layers-1 else None,
                          use_checkpoint=use_checkpoint,
                          out_dim=embed_dims[min(i_layer+1, len(embed_dims)-1)],
                          activation=activation)
            if is_conv:
                layer = ConvLayer(conv_expand_ratio=mbconv_expand_ratio, **kwargs)
            else:
                layer = BasicLayer(num_heads=num_heads[i_layer],
                                   window_size=window_sizes[i_layer],
                                   mlp_ratio=mlp_ratio,
                                   drop=drop_rate,
                                   local_conv_size=local_conv_size,
                                   **kwargs)
            self.layers.append(layer)

        # Head
        self.norm_head = nn.LayerNorm(embed_dims[-1])
        self.head = nn.Linear(embed_dims[-1], num_classes) if num_classes>0 else nn.Identity()

        # Neck for Feature Output
        self.neck = nn.Sequential(
            nn.Conv2d(embed_dims[-1],256,1,bias=False),
            LayerNorm2d(256),
            nn.Conv2d(256,256,3,padding=1,bias=False),
            LayerNorm2d(256),
        )

        self.apply(self._init_weights)
        self.set_layer_lr_decay(layer_lr_decay)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None: nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0); nn.init.constant_(m.weight, 1.0)

    def set_layer_lr_decay(self, layer_lr_decay):
        decay_rate = layer_lr_decay
        depth = sum(self.depths)
        lr_scales = [decay_rate ** (depth - i - 1) for i in range(depth)]
        def _set_lr_scale(m, scale):
            for p in m.parameters(): p.lr_scale = scale

        # Patch Embedding
        self.patch_embed.apply(lambda x: _set_lr_scale(x, lr_scales[0]))
        i = 0
        for layer in self.layers:
            for block in layer.blocks:
                block.apply(lambda x: _set_lr_scale(x, lr_scales[i])); i+=1
            if layer.downsample is not None:
                layer.downsample.apply(lambda x: _set_lr_scale(x, lr_scales[i-1]))
        assert i == depth
        for m in [self.norm_head, self.head]:
            m.apply(lambda x: _set_lr_scale(x, lr_scales[-1]))
        for k,p in self.named_parameters():
            p.param_name = k
        self.apply(lambda m: [hasattr(p,'lr_scale') or AssertionError(p.param_name) for p in m.parameters()])

    def forward_features(self, x):
        x = self.patch_embed(x)
        for layer in self.layers:
            x = layer(x)
        B,_,C = x.size()
        x = x.view(B,64,64,C).permute(0,3,1,2)
        x = self.neck(x)

        return x

    def forward(self, x):
        return self.forward_features(x)
