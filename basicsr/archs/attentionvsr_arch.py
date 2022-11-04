import torch
from torch import nn as nn
from torch.nn import functional as F

from basicsr.utils.registry import ARCH_REGISTRY
from .arch_util import ResidualBlockNoBN, flow_warp, make_layer
from .spynet_arch import SpyNet
from positional_encodings import PositionalEncodingPermute3D


@ARCH_REGISTRY.register()
class BasicAttention_VSR(nn.Module):
    def __init__(self,
                 image_ch=3,
                 num_feat=64,
                 feat_size=64,
                 num_frame=7,
                 num_extract_block=5,
                 depth=2,
                 heads=1,
                 patch_size=8,
                 num_block=15,
                 spynet_path=None):
        super().__init__()
        self.num_feat = num_feat

        # Attention
        self.center_frame_idx = num_frame // 2
        self.num_frame = num_frame

        # Feature extractor
        self.conv_first = nn.Conv2d(image_ch, num_feat, 3, 1, 1)
        self.feature_extraction = make_layer(ResidualBlockNoBN, num_extract_block, num_feat=num_feat)
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

        # Transformer
        self.pos_embedding = PositionalEncodingPermute3D(num_frame)
        self.transformer = Transformer(num_feat, feat_size, depth, patch_size, heads)

        # alignment
        self.spynet = SpyNet(spynet_path)

        # propagation
        self.backward_trunk = ConvResidualBlocks(num_feat + 3, num_feat, num_block)
        self.forward_trunk = ConvResidualBlocks(num_feat + 3, num_feat, num_block)

        # reconstruction
        self.fusion = nn.Conv2d(num_feat * 3, num_feat, 1, 1, 0, bias=True)
        self.upconv1 = nn.Conv2d(num_feat, num_feat * 4, 3, 1, 1, bias=True)
        self.upconv2 = nn.Conv2d(num_feat, 64 * 4, 3, 1, 1, bias=True)
        self.conv_hr = nn.Conv2d(64, 64, 3, 1, 1)
        self.conv_last = nn.Conv2d(64, 3, 3, 1, 1)

        self.pixel_shuffle = nn.PixelShuffle(2)

        # activation functions
        self.lrelu = nn.LeakyReLU(negative_slope=0.1, inplace=True)

    def get_flow(self, x):
        b, n, c, h, w = x.size()

        x_1 = x[:, :-1, :, :, :].reshape(-1, c, h, w)
        x_2 = x[:, 1:, :, :, :].reshape(-1, c, h, w)

        flows_backward = self.spynet(x_1, x_2).view(b, n - 1, 2, h, w)
        flows_forward = self.spynet(x_2, x_1).view(b, n - 1, 2, h, w)

        return flows_forward, flows_backward

    def get_attention(self, x):
        b, n, c, h, w = x.size()
        # extract features for each frame
        feat = self.lrelu(self.conv_first(x.view(-1, c, h, w)))  # [B*5, 64, 64, 64]
        # print(feat.shape)
        feat = self.feature_extraction(feat).view(b, n, -1, h, w)  # [B, 5, 64, 64, 64]
        print(feat.shape)
        # transformer

        feat = feat + self.pos_embedding(feat)  # [B, 5, 64, 64, 64]
        # tr_feat = self.transformer(feat)                                 # [B, 5, 64, 64, 64]
        attention_feat = self.transformer(feat)  # [B, 5, 64, 64, 64]
        # attention_feat = tr_feat.view(b, -1, h, w)                     # [B, n*, 64, 64]
        # print(attention_feat.shape)
        return attention_feat

    def forward(self, x):
        flows_forward, flows_backward = self.get_flow(x)
        b, n, c, h, w = x.size()
        attention_feat = self.get_attention(x)
        # backward branch
        out_l = []
        feat_prop = x.new_zeros(b, self.num_feat, h, w)
        for i in range(n - 1, -1, -1):
            x_i = x[:, i, :, :, :]
            # print('test_backwardbranch')
            # print(x_i.shape)
            if i < n - 1:
                flow = flows_backward[:, i, :, :, :]
                feat_prop = flow_warp(feat_prop, flow.permute(0, 2, 3, 1))
            feat_prop = torch.cat([x_i, feat_prop], dim=1)
            feat_prop = self.backward_trunk(feat_prop)
            out_l.insert(0, feat_prop)
        out_o = torch.stack(out_l, dim=1)  # [b, 14, 64, 64, 64] tensor
        out_o = torch.cat([out_o, attention_feat], dim=2)  # [b, 14, 128, 64, 64] tensor
        # print('out_o shape')
        # print(out_o.shape)

        # forward branch
        feat_prop = torch.zeros_like(feat_prop)
        for i in range(0, n):
            x_i = x[:, i, :, :, :]
            if i > 0:
                flow = flows_forward[:, i - 1, :, :, :]
                feat_prop = flow_warp(feat_prop, flow.permute(0, 2, 3, 1))

            feat_prop = torch.cat([x_i, feat_prop], dim=1)
            feat_prop = self.forward_trunk(feat_prop)

            # upsample
            out = torch.cat([out_o[:, i, ...], feat_prop], dim=1)
            out = self.lrelu(self.fusion(out))
            out = self.lrelu(self.pixel_shuffle(self.upconv1(out)))
            out = self.lrelu(self.pixel_shuffle(self.upconv2(out)))
            out = self.lrelu(self.conv_hr(out))
            out = self.conv_last(out)
            base = F.interpolate(x_i, scale_factor=4, mode='bilinear', align_corners=False)
            out += base
            out_l[i] = out
        outo = torch.stack(out_l, dim=1)
        # print(outo.shape)

        return torch.stack(out_l, dim=1)


class ConvResidualBlocks(nn.Module):

    def __init__(self, num_in_ch=3, num_out_ch=64, num_block=15):
        super().__init__()
        self.main = nn.Sequential(
            nn.Conv2d(num_in_ch, num_out_ch, 3, 1, 1, bias=True), nn.LeakyReLU(negative_slope=0.1, inplace=True),
            make_layer(ResidualBlockNoBN, num_block, num_feat=num_out_ch))

    def forward(self, fea):
        return self.main(fea)


# Attention
class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(x, **kwargs) + x


class PreNorm(nn.Module):
    def __init__(self, num_feat, feat_size, fn):
        super().__init__()
        self.norm = nn.LayerNorm([num_feat, feat_size, feat_size])
        self.fn = fn

    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)


class Identity(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x


class MatmulNet(nn.Module):
    def __init__(self) -> None:
        super(MatmulNet, self).__init__()

    def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        x = torch.matmul(x, y)
        return x


class globalAttention(nn.Module):
    def __init__(self, num_feat=64, patch_size=8, heads=1):
        super(globalAttention, self).__init__()
        self.heads = heads
        self.dim = patch_size ** 2 * num_feat
        self.hidden_dim = self.dim // heads
        self.num_patch = (64 // patch_size) ** 2

        self.to_q = nn.Conv2d(in_channels=num_feat, out_channels=num_feat, kernel_size=3, padding=1, groups=num_feat)
        self.to_k = nn.Conv2d(in_channels=num_feat, out_channels=num_feat, kernel_size=3, padding=1, groups=num_feat)
        self.to_v = nn.Conv2d(in_channels=num_feat, out_channels=num_feat, kernel_size=3, padding=1)

        self.conv = nn.Conv2d(in_channels=num_feat, out_channels=num_feat, kernel_size=3, padding=1)

        self.feat2patch = torch.nn.Unfold(kernel_size=patch_size, padding=0, stride=patch_size)
        self.patch2feat = torch.nn.Fold(output_size=(64, 64), kernel_size=patch_size, padding=0, stride=patch_size)

    def forward(self, x):
        b, t, c, h, w = x.shape  # B, 5, 64, 64, 64
        H, D = self.heads, self.dim
        n, d = self.num_patch, self.hidden_dim

        q = self.to_q(x.view(-1, c, h, w))  # [B*5, 64, 64, 64]
        k = self.to_k(x.view(-1, c, h, w))  # [B*5, 64, 64, 64]
        v = self.to_v(x.view(-1, c, h, w))  # [B*5, 64, 64, 64]

        unfold_q = self.feat2patch(q)  # [B*5, 8*8*64, 8*8]
        unfold_k = self.feat2patch(k)  # [B*5, 8*8*64, 8*8]
        unfold_v = self.feat2patch(v)  # [B*5, 8*8*64, 8*8]

        unfold_q = unfold_q.view(b, t, H, d, n)  # [B, 5, H, 8*8*64/H, 8*8]
        unfold_k = unfold_k.view(b, t, H, d, n)  # [B, 5, H, 8*8*64/H, 8*8]
        unfold_v = unfold_v.view(b, t, H, d, n)  # [B, 5, H, 8*8*64/H, 8*8]

        unfold_q = unfold_q.permute(0, 2, 3, 1, 4).contiguous()  # [B, H, 8*8*64/H, 5, 8*8]
        unfold_k = unfold_k.permute(0, 2, 3, 1, 4).contiguous()  # [B, H, 8*8*64/H, 5, 8*8]
        unfold_v = unfold_v.permute(0, 2, 3, 1, 4).contiguous()  # [B, H, 8*8*64/H, 5, 8*8]

        unfold_q = unfold_q.view(b, H, d, t * n)  # [B, H, 8*8*64/H, 5*8*8]
        unfold_k = unfold_k.view(b, H, d, t * n)  # [B, H, 8*8*64/H, 5*8*8]
        unfold_v = unfold_v.view(b, H, d, t * n)  # [B, H, 8*8*64/H, 5*8*8]

        attn = torch.matmul(unfold_q.transpose(2, 3), unfold_k)  # [B, H, 5*8*8, 5*8*8]
        attn = attn * (d ** (-0.5))  # [B, H, 5*8*8, 5*8*8]
        attn = F.softmax(attn, dim=-1)  # [B, H, 5*8*8, 5*8*8]

        attn_x = torch.matmul(attn, unfold_v.transpose(2, 3))  # [B, H, 5*8*8, 8*8*64/H]
        attn_x = attn_x.view(b, H, t, n, d)  # [B, H, 5, 8*8, 8*8*64/H]
        attn_x = attn_x.permute(0, 2, 1, 4, 3).contiguous()  # [B, 5, H, 8*8*64/H, 8*8]
        attn_x = attn_x.view(b * t, D, n)  # [B*5, 8*8*64, 8*8]
        feat = self.patch2feat(attn_x)  # [B*5, 64, 64, 64]

        out = self.conv(feat).view(x.shape)  # [B, 5, 64, 64, 64]
        out += x  # [B, 5, 64, 64, 64]

        return out


class Transformer(nn.Module):
    def __init__(self, num_feat, feat_size, depth, patch_size, heads):
        super().__init__()
        self.layers = nn.ModuleList([])
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                Residual(PreNorm(num_feat, feat_size, globalAttention(num_feat, patch_size, heads))),
                Residual(PreNorm(num_feat, feat_size, globalAttention(num_feat, patch_size, heads)))
            ]))

    def forward(self, x):
        for attn, attn1 in self.layers:
            x = attn(x)
            x = attn1(x)
        return x
