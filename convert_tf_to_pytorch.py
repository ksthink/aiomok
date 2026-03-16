# -*- coding: utf-8 -*-
"""
Convert AlphaZero_Gomoku_MPI TensorFlow checkpoint to PyTorch state_dict.

Network architecture (from policy_value_net_tensorlayer.py):
  - Input: (batch, 9, 15, 15) → ZeroPad2d(2) → (batch, 9, 19, 19)
  - conv2d_1: Conv2d(9→64, k=1x1) + bias (no BN)
  - 19 ResNet blocks: Conv(64→64, k=3, pad=SAME) + BN + ReLU + Conv + BN + skip + ReLU
  - Policy head: Conv2d(64→2, k=1) + BN(ReLU) + Flatten(722) → Dense(722→225, log_softmax)
  - Value head: Conv2d(64→1, k=1) + BN(ReLU) + Flatten(361) → Dense(361→256, ReLU) → Dense(256→1, tanh)

TF key patterns:
  model/conv2d_1/{kernel,bias}
  model/resnet_conv2d_{i}_1/{kernel,bias}     i=0..18
  model/resnet_bn_{i}_1/{gamma,beta,moving_mean,moving_variance}
  model/resnet_conv2d_{i}_2/{kernel,bias}
  model/resnet_bn_{i}_2/{gamma,beta,moving_mean,moving_variance}
  model/conv2d_2/{kernel,bias}   → policy conv
  model/bn_1/{gamma,beta,moving_mean,moving_variance}  → policy BN
  model/dense_layer_1/{W,b}     → policy FC
  model/conv2d_3/{kernel,bias}   → value conv
  model/bn_2/{gamma,beta,moving_mean,moving_variance}  → value BN
  model/dense_layer_2/{W,b}     → value FC1
  model/flatten_layer_3/{W,b}   → value FC2
"""

import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


# ─── PyTorch Network Definition ─────────────────────────

class AlphaZeroResBlock(nn.Module):
    def __init__(self, channels=64):
        super().__init__()
        self.conv1 = nn.Conv2d(channels, channels, 3, padding=1, bias=True)
        self.bn1   = nn.BatchNorm2d(channels)
        self.conv2 = nn.Conv2d(channels, channels, 3, padding=1, bias=True)
        self.bn2   = nn.BatchNorm2d(channels)

    def forward(self, x):
        residual = x
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return F.relu(out + residual)


class AlphaZeroNet(nn.Module):
    """
    AlphaZero network for 15x15 Gomoku.
    Input: (batch, 9, 15, 15)
    Output: policy_log_probs (batch, 225), value (batch, 1)
    """
    def __init__(self, in_channels=9, num_channels=64, num_res_blocks=19,
                 board_size=15, padded_size=19):
        super().__init__()
        self.board_size = board_size
        self.padded_size = padded_size
        self.pad = 2  # ZeroPad2d amount

        # Initial 1x1 conv (applied after zero-padding to 19x19)
        self.conv_init = nn.Conv2d(in_channels, num_channels, 1, bias=True)

        # 19 residual blocks
        self.res_blocks = nn.ModuleList(
            [AlphaZeroResBlock(num_channels) for _ in range(num_res_blocks)]
        )

        # Policy head
        self.policy_conv = nn.Conv2d(num_channels, 2, 1, bias=True)
        self.policy_bn   = nn.BatchNorm2d(2)
        self.policy_fc   = nn.Linear(2 * padded_size * padded_size, board_size * board_size)

        # Value head
        self.value_conv = nn.Conv2d(num_channels, 1, 1, bias=True)
        self.value_bn   = nn.BatchNorm2d(1)
        self.value_fc1  = nn.Linear(1 * padded_size * padded_size, 256)
        self.value_fc2  = nn.Linear(256, 1)

    def forward(self, x):
        # x: (batch, 9, 15, 15)
        x = F.pad(x, (self.pad, self.pad, self.pad, self.pad))  # → (batch, 9, 19, 19)
        x = self.conv_init(x)  # → (batch, 64, 19, 19), no BN/ReLU after init conv

        for block in self.res_blocks:
            x = block(x)

        # Policy head
        p = F.relu(self.policy_bn(self.policy_conv(x)))
        p = p.flatten(1)  # (batch, 2*19*19=722)
        p = F.log_softmax(self.policy_fc(p), dim=1)  # (batch, 225)

        # Value head
        v = F.relu(self.value_bn(self.value_conv(x)))
        v = v.flatten(1)  # (batch, 1*19*19=361)
        v = F.relu(self.value_fc1(v))
        v = torch.tanh(self.value_fc2(v))  # (batch, 1)

        return p, v


# ─── TF → PyTorch conversion ────────────────────────────

def load_tf_checkpoint(ckpt_path):
    """Load TF checkpoint variables into a dict."""
    import tensorflow as tf
    reader = tf.train.load_checkpoint(ckpt_path)
    var_to_shape = reader.get_variable_to_shape_map()
    tf_vars = {}
    for name in sorted(var_to_shape.keys()):
        tf_vars[name] = reader.get_tensor(name)
    return tf_vars


def conv_kernel_tf_to_pt(kernel):
    """TF conv kernel [H,W,I,O] → PyTorch [O,I,H,W]"""
    return np.transpose(kernel, (3, 2, 0, 1))


def dense_weight_tf_to_pt(w):
    """TF Dense W [in,out] → PyTorch Linear weight [out,in]"""
    return w.T


def convert(tf_vars, net):
    """Map TF checkpoint variables to PyTorch state dict."""
    sd = net.state_dict()

    # Helper: set a param
    def set_param(pt_key, np_val):
        t = torch.from_numpy(np_val.copy()).float()
        assert sd[pt_key].shape == t.shape, \
            f"Shape mismatch: {pt_key} expected {sd[pt_key].shape}, got {t.shape}"
        sd[pt_key] = t

    # 1) Initial conv (1x1)
    set_param('conv_init.weight', conv_kernel_tf_to_pt(tf_vars['model/conv2d_1/kernel']))
    set_param('conv_init.bias', tf_vars['model/conv2d_1/bias'])

    # 2) 19 residual blocks
    for i in range(19):
        # First conv + BN in block
        set_param(f'res_blocks.{i}.conv1.weight',
                  conv_kernel_tf_to_pt(tf_vars[f'model/resnet_conv2d_{i}_1/kernel']))
        set_param(f'res_blocks.{i}.conv1.bias',
                  tf_vars[f'model/resnet_conv2d_{i}_1/bias'])
        set_param(f'res_blocks.{i}.bn1.weight',
                  tf_vars[f'model/resnet_bn_{i}_1/gamma'])
        set_param(f'res_blocks.{i}.bn1.bias',
                  tf_vars[f'model/resnet_bn_{i}_1/beta'])
        set_param(f'res_blocks.{i}.bn1.running_mean',
                  tf_vars[f'model/resnet_bn_{i}_1/moving_mean'])
        set_param(f'res_blocks.{i}.bn1.running_var',
                  tf_vars[f'model/resnet_bn_{i}_1/moving_variance'])

        # Second conv + BN in block
        set_param(f'res_blocks.{i}.conv2.weight',
                  conv_kernel_tf_to_pt(tf_vars[f'model/resnet_conv2d_{i}_2/kernel']))
        set_param(f'res_blocks.{i}.conv2.bias',
                  tf_vars[f'model/resnet_conv2d_{i}_2/bias'])
        set_param(f'res_blocks.{i}.bn2.weight',
                  tf_vars[f'model/resnet_bn_{i}_2/gamma'])
        set_param(f'res_blocks.{i}.bn2.bias',
                  tf_vars[f'model/resnet_bn_{i}_2/beta'])
        set_param(f'res_blocks.{i}.bn2.running_mean',
                  tf_vars[f'model/resnet_bn_{i}_2/moving_mean'])
        set_param(f'res_blocks.{i}.bn2.running_var',
                  tf_vars[f'model/resnet_bn_{i}_2/moving_variance'])

    # 3) Policy head
    set_param('policy_conv.weight',
              conv_kernel_tf_to_pt(tf_vars['model/conv2d_2/kernel']))
    set_param('policy_conv.bias', tf_vars['model/conv2d_2/bias'])
    set_param('policy_bn.weight', tf_vars['model/bn_1/gamma'])
    set_param('policy_bn.bias', tf_vars['model/bn_1/beta'])
    set_param('policy_bn.running_mean', tf_vars['model/bn_1/moving_mean'])
    set_param('policy_bn.running_var', tf_vars['model/bn_1/moving_variance'])
    set_param('policy_fc.weight',
              dense_weight_tf_to_pt(tf_vars['model/dense_layer_1/W']))
    set_param('policy_fc.bias', tf_vars['model/dense_layer_1/b'])

    # 4) Value head
    set_param('value_conv.weight',
              conv_kernel_tf_to_pt(tf_vars['model/conv2d_3/kernel']))
    set_param('value_conv.bias', tf_vars['model/conv2d_3/bias'])
    set_param('value_bn.weight', tf_vars['model/bn_2/gamma'])
    set_param('value_bn.bias', tf_vars['model/bn_2/beta'])
    set_param('value_bn.running_mean', tf_vars['model/bn_2/moving_mean'])
    set_param('value_bn.running_var', tf_vars['model/bn_2/moving_variance'])
    set_param('value_fc1.weight',
              dense_weight_tf_to_pt(tf_vars['model/dense_layer_2/W']))
    set_param('value_fc1.bias', tf_vars['model/dense_layer_2/b'])
    set_param('value_fc2.weight',
              dense_weight_tf_to_pt(tf_vars['model/flatten_layer_3/W']))
    set_param('value_fc2.bias', tf_vars['model/flatten_layer_3/b'])

    return sd


def main():
    model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model')
    ckpt_path = os.path.join(model_dir, 'best_policy.model')
    output_path = os.path.join(model_dir, 'alphazero_15x15.pt')

    print(f"Loading TF checkpoint from: {ckpt_path}")
    tf_vars = load_tf_checkpoint(ckpt_path)
    print(f"Loaded {len(tf_vars)} TF variables")

    # Print all variable names and shapes for verification
    for name, val in sorted(tf_vars.items()):
        print(f"  {name}: {val.shape}")

    print("\nCreating PyTorch network...")
    net = AlphaZeroNet(in_channels=9, num_channels=64, num_res_blocks=19,
                       board_size=15, padded_size=19)

    print("Converting weights...")
    sd = convert(tf_vars, net)
    net.load_state_dict(sd)
    net.eval()

    # Quick sanity check: forward pass with random input
    print("\nSanity check: forward pass with random input...")
    with torch.no_grad():
        dummy = torch.randn(1, 9, 15, 15)
        policy, value = net(dummy)
        print(f"  Policy shape: {policy.shape}, sum(exp(policy)): {policy.exp().sum().item():.4f}")
        print(f"  Value shape: {value.shape}, value: {value.item():.4f}")

    # Save
    torch.save(sd, output_path)
    print(f"\nSaved PyTorch state_dict to: {output_path}")
    print(f"File size: {os.path.getsize(output_path) / 1024 / 1024:.1f} MB")


if __name__ == '__main__':
    main()
