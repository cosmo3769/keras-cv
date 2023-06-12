# Copyright 2022 The KerasCV Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""RegNet models for KerasCV.
References:
    - [Designing Network Design Spaces](https://arxiv.org/abs/2003.13678)
    (CVPR 2020)
    - [Based on the Original keras.applications RegNet](https://github.com/keras-team/keras/blob/master/keras/applications/regnet.py)
"""  # noqa: E501

import copy

from tensorflow import keras
from tensorflow.keras import backend
from tensorflow.keras import layers

from keras_cv.layers import SqueezeAndExcite2D
from keras_cv.models import utils
from keras_cv.models.backbones.backbone import Backbone
from keras_cv.models.backbones.regnet.regnetx_backbone_presets import (
    backbone_presets_x,
)
from keras_cv.models.backbones.regnet.regnety_backbone_presets import (
    backbone_presets_y,
)
from keras_cv.utils.python_utils import classproperty

# BatchNorm momentum and epsilon values taken from original implementation.

BASE_DOCSTRING = """This class represents the {name} architecture.

  Reference:
    - [Designing Network Design Spaces](https://arxiv.org/abs/2003.13678)
    (CVPR 2020)

  For image classification use cases, see [this page for detailed examples](https://keras.io/api/applications/#usage-examples-for-image-classification-models).

  For transfer learning use cases, make sure to read the [guide to transfer learning & fine-tuning](https://keras.io/guides/transfer_learning/).


  The naming of models is as follows: `RegNet<block_type><flops>` where
  `block_type` is one of `(X, Y)` and `flops` signifies hundred million
  floating point operations. For example RegNetY064 corresponds to RegNet with
  Y block and 6.4 giga flops (64 hundred million flops).

  Args:
    include_rescaling: whether or not to Rescale the inputs.If set to True,
        inputs will be passed through a `Rescaling(1/255.0)` layer.
    input_tensor: Optional Keras tensor (i.e. output of `layers.Input()`)
        to use as image input for the model.
    input_shape: Optional shape tuple, defaults to (None, None, 3).
        It should have exactly 3 inputs channels.
"""  # noqa: E501


def apply_conv2d_bn(
    x,
    filters,
    kernel_size,
    strides=1,
    use_bias=False,
    groups=1,
    padding="valid",
    kernel_initializer="he_normal",
    batch_norm=True,
    activation="relu",
    name="",
):
    x = layers.Conv2D(
        filters,
        kernel_size,
        strides=strides,
        groups=groups,
        use_bias=use_bias,
        padding=padding,
        kernel_initializer=kernel_initializer,
        name=name,
    )(x)

    if batch_norm:
        x = layers.BatchNormalization(
            momentum=0.9, epsilon=1e-5, name=name + "_bn"
        )(x)

    if activation is not None:
        x = layers.Activation(activation, name=name + f"_{activation}")(x)

    return x


def apply_stem(x, name=None):
    """Implementation of RegNet stem.

    (Common to all model variants)
    Args:
      x: Tensor, input tensor to the stem
      name: name prefix

    Returns:
      Output tensor of the Stem
    """
    if name is None:
        name = "stem" + str(backend.get_uid("stem"))

    x = apply_conv2d_bn(
        x=x,
        filters=32,
        kernel_size=(3, 3),
        strides=2,
        padding="same",
        name=name + "_stem_conv",
    )

    return x


def apply_x_block(
    inputs, filters_in, filters_out, group_width, stride=1, name=None
):
    """Implementation of X Block.
    References:
        - [Designing Network Design Spaces](https://arxiv.org/abs/2003.13678)

    Args:
      inputs: Tensor, input tensor to the block
      filters_in: int, filters in the input tensor
      filters_out: int, filters in the output tensor
      group_width: int, group width
      stride: int (or) tuple, stride of Conv layer
      name: str, name prefix
    Returns:
      Output tensor of the block
    """
    if name is None:
        name = str(backend.get_uid("xblock"))

    if filters_in != filters_out and stride == 1:
        raise ValueError(
            f"Input filters({filters_in}) and output "
            f"filters({filters_out}) "
            f"are not equal for stride {stride}. Input and output filters "
            f"must be equal for stride={stride}."
        )

    # Declare layers
    groups = filters_out // group_width

    if stride != 1:
        skip = apply_conv2d_bn(
            x=inputs,
            filters=filters_out,
            kernel_size=(1, 1),
            strides=stride,
            activation=None,
            name=name + "_skip_1x1",
        )
    else:
        skip = inputs

    # Build block
    # conv_1x1_1
    x = apply_conv2d_bn(
        x=inputs,
        filters=filters_out,
        kernel_size=(1, 1),
        name=name + "_conv_1x1_1",
    )

    # conv_3x3
    x = apply_conv2d_bn(
        x=x,
        filters=filters_out,
        kernel_size=(3, 3),
        strides=stride,
        groups=groups,
        padding="same",
        name=name + "_conv_3x3",
    )

    # conv_1x1_2
    x = apply_conv2d_bn(
        x=x,
        filters=filters_out,
        kernel_size=(1, 1),
        activation=None,
        name=name + "_conv_1x1_2",
    )

    x = layers.Activation("relu", name=name + "_exit_relu")(x + skip)

    return x


def apply_y_block(
    inputs,
    filters_in,
    filters_out,
    group_width,
    stride=1,
    squeeze_excite_ratio=0.25,
    name=None,
):
    """Implementation of Y Block.
    References:
        - [Designing Network Design Spaces](https://arxiv.org/abs/2003.13678)

    Args:
      inputs: Tensor, input tensor to the block
      filters_in: int, filters in the input tensor
      filters_out: int, filters in the output tensor
      group_width: int, group width
      stride: int (or) tuple, stride of Conv layer
      squeeze_excite_ratio: float, expansion ratio for Squeeze and Excite block
      name: str, name prefix
    Returns:
      Output tensor of the block
    """
    if name is None:
        name = str(backend.get_uid("yblock"))

    if filters_in != filters_out and stride == 1:
        raise ValueError(
            f"Input filters({filters_in}) and output "
            f"filters({filters_out}) "
            f"are not equal for stride {stride}. Input and output filters "
            f"must be equal for stride={stride}."
        )

    groups = filters_out // group_width

    if stride != 1:
        skip = apply_conv2d_bn(
            x=inputs,
            filters=filters_out,
            kernel_size=(1, 1),
            strides=stride,
            activation=None,
            name=name + "_skip_1x1",
        )
    else:
        skip = inputs

    # Build block
    # conv_1x1_1
    x = apply_conv2d_bn(
        x=inputs,
        filters=filters_out,
        kernel_size=(1, 1),
        name=name + "_conv_1x1_1",
    )

    # conv_3x3
    x = apply_conv2d_bn(
        x=x,
        filters=filters_out,
        kernel_size=(3, 3),
        strides=stride,
        groups=groups,
        padding="same",
        name=name + "_conv_3x3",
    )

    # Squeeze-Excitation block
    x = SqueezeAndExcite2D(filters_out, ratio=squeeze_excite_ratio, name=name)(
        x
    )

    # conv_1x1_2
    x = apply_conv2d_bn(
        x=x,
        filters=filters_out,
        kernel_size=(1, 1),
        activation=None,
        name=name + "_conv_1x1_2",
    )

    x = layers.Activation("relu", name=name + "_exit_relu")(x + skip)

    return x


def apply_z_block(
    inputs,
    filters_in,
    filters_out,
    group_width,
    stride=1,
    squeeze_excite_ratio=0.25,
    bottleneck_ratio=0.25,
    name=None,
):
    """Implementation of Z block.

    References:
        - [Fast and Accurate Model Scaling](https://arxiv.org/abs/2103.06877).

    Args:
      inputs: Tensor, input tensor to the block
      filters_in: int, filters in the input tensor
      filters_out: int, filters in the output tensor
      group_width: int, group width
      stride: int (or) tuple, stride
      squeeze_excite_ratio: float, expansion ration for Squeeze and Excite block
      bottleneck_ratio: float, inverted bottleneck ratio
      name: str, name prefix
    Returns:
      Output tensor of the block
    """
    if name is None:
        name = str(backend.get_uid("zblock"))

    if filters_in != filters_out and stride == 1:
        raise ValueError(
            f"Input filters({filters_in}) and output filters({filters_out})"
            f"are not equal for stride {stride}. Input and output filters "
            f"must be equal for stride={stride}."
        )

    groups = filters_out // group_width

    inv_btlneck_filters = int(filters_out / bottleneck_ratio)

    # Build block
    # conv_1x1_1
    x = apply_conv2d_bn(
        x=inputs,
        filters=inv_btlneck_filters,
        kernel_size=(1, 1),
        name=name + "_conv_1x1_1",
        activation="silu",
    )

    # conv_3x3
    x = apply_conv2d_bn(
        x=x,
        filters=inv_btlneck_filters,
        kernel_size=(3, 3),
        strides=stride,
        groups=groups,
        padding="same",
        name=name + "_conv_3x3",
        activation="silu",
    )

    # Squeeze-Excitation block
    x = SqueezeAndExcite2D(
        inv_btlneck_filters, ratio=squeeze_excite_ratio, name=name
    )(x)

    # conv_1x1_2
    x = apply_conv2d_bn(
        x=x,
        filters=filters_out,
        kernel_size=(1, 1),
        activation=None,
        name=name + "_conv_1x1_2",
    )

    if stride != 1:
        return x
    else:
        return x + inputs


def apply_stage(
    x, block_type, depth, group_width, filters_in, filters_out, name=None
):
    """Implementation of Stage in RegNet.

    Args:
      x: Tensor, input tensor to the stage
      block_type: must be one of "X", "Y", "Z"
      depth: int, depth of stage, number of blocks to use
      group_width: int, group width of all blocks in  this stage
      filters_in: int, input filters to this stage
      filters_out: int, output filters from this stage
      name: str, name prefix

    Returns:
      Output tensor of the block
    """
    if name is None:
        name = str(backend.get_uid("stage"))

    if block_type == "X":
        x = apply_x_block(
            x,
            filters_in,
            filters_out,
            group_width,
            stride=2,
            name=f"{name}_XBlock_0",
        )
        for i in range(1, depth):
            x = apply_x_block(
                x,
                filters_out,
                filters_out,
                group_width,
                name=f"{name}_XBlock_{i}",
            )
    elif block_type == "Y":
        x = apply_y_block(
            x,
            filters_in,
            filters_out,
            group_width,
            stride=2,
            name=name + "_YBlock_0",
        )
        for i in range(1, depth):
            x = apply_y_block(
                x,
                filters_out,
                filters_out,
                group_width,
                name=f"{name}_YBlock_{i}",
            )
    elif block_type == "Z":
        x = apply_z_block(
            x,
            filters_in,
            filters_out,
            group_width,
            stride=2,
            name=f"{name}_ZBlock_0",
        )
        for i in range(1, depth):
            x = apply_z_block(
                x,
                filters_out,
                filters_out,
                group_width,
                name=f"{name}_ZBlock_{i}",
            )
    else:
        raise NotImplementedError(
            f"Block type `{block_type}` not recognized."
            f"block_type must be one of (`X`, `Y`, `Z`). "
        )
    return x


@keras.utils.register_keras_serializable(package="keras_cv.models")
class RegNetBackBone(Backbone):
    """
    This class represents the architecture of RegNet
    Args:
        depths: iterable, Contains depths for each individual stages.
        widths: iterable, Contains output channel width of each individual
            stages
        group_width: int, Number of channels to be used in each group. See grouped
            convolutions for more information.
        block_type: Must be one of `{"X", "Y", "Z"}`. For more details see the
            papers "Designing network design spaces" and "Fast and Accurate
            Model Scaling"
        default_size: tuple (or) list, Default input image size.
        model_name: str, An optional name for the model.
        include_rescaling: bool, whether or not to Rescale the inputs.If set to True,
            inputs will be passed through a `Rescaling(1/255.0)` layer.
        input_tensor: Tensor, Optional Keras tensor (i.e. output of `layers.Input()`)
            to use as image input for the model.
        input_shape: Optional shape tuple, defaults to (None, None, 3).
            It should have exactly 3 inputs channels.
    """  # noqa: E501

    def __init__(
        self,
        *,
        depths,
        widths,
        group_width,
        block_type,
        include_rescaling,
        model_name="regnet",
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        img_input = utils.parse_model_inputs(input_shape, input_tensor)
        x = img_input

        if include_rescaling:
            x = layers.Rescaling(scale=1.0 / 255.0)(x)
        x = apply_stem(x, name=model_name)

        in_channels = x.shape[-1]  # Output from Stem

        NUM_STAGES = 4

        for stage_index in range(NUM_STAGES):
            depth = depths[stage_index]
            out_channels = widths[stage_index]

            x = apply_stage(
                x,
                block_type,
                depth,
                group_width,
                in_channels,
                out_channels,
                name=model_name + "_Stage_" + str(stage_index),
            )
            in_channels = out_channels

        super().__init__(inputs=img_input, outputs=x, name=model_name, **kwargs)

        self.depths = depths
        self.widths = widths
        self.group_width = group_width
        self.block_type = block_type
        self.include_rescaling = include_rescaling
        self.model_name = model_name
        self.input_tensor = input_tensor

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "depths": self.depths,
                "widths": self.widths,
                "group_width": self.group_width,
                "block_type": self.block_type,
                "include_rescaling": self.include_rescaling,
                "model_name": self.model_name,
                "input_tensor": self.input_tensor,
                "input_shape": self.input_shape[1:],
            }
        )

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return copy.deepcopy(backbone_presets_x)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return copy.deepcopy(backbone_presets_y)


# Instantiating variants
class RegNetX002Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx002", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX004Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx004", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX006Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx006", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX008Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx008", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX016Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx016", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX032Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx032", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX040Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx040", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX064Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx064", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX080Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx080", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX120Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx120", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX160Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx160", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetX320Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnetx320", **kwargs)

    @classproperty
    def presetsx(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY002Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety002", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY004Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety004", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY006Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety006", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY008Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety008", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY016Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety016", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY032Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety032", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY040Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety040", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY064Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety064", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY080Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety080", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY120Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety120", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY160Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety160", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


class RegNetY320Backbone(RegNetBackBone):
    def __new__(
        cls,
        include_rescaling=True,
        input_tensor=None,
        input_shape=(None, None, 3),
        **kwargs,
    ):
        # Pack args in kwargs
        kwargs.update(
            {
                "include_rescaling": include_rescaling,
                "input_tensor": input_tensor,
                "input_shape": input_shape,
            }
        )
        return RegNetBackBone.from_preset("regnety320", **kwargs)

    @classproperty
    def presetsy(cls):
        """Dictionary of preset names and configurations."""
        return {}


RegNetX002Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX002Backbone")
RegNetX004Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX004Backbone")
RegNetX006Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX006Backbone")
RegNetX008Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX008Backbone")
RegNetX016Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX016Backbone")
RegNetX032Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX032Backbone")
RegNetX040Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX040Backbone")
RegNetX064Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX064Backbone")
RegNetX080Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX080Backbone")
RegNetX120Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX120Backbone")
RegNetX160Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX160Backbone")
RegNetX320Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetX320Backbone")

RegNetY002Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY002Backbone")
RegNetY004Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY004Backbone")
RegNetY006Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY006Backbone")
RegNetY008Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY008Backbone")
RegNetY016Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY016Backbone")
RegNetY032Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY032Backbone")
RegNetY040Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY040Backbone")
RegNetY064Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY064Backbone")
RegNetY080Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY080Backbone")
RegNetY120Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY120Backbone")
RegNetY160Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY160Backbone")
RegNetY320Backbone.__doc__ = BASE_DOCSTRING.format(name="RegNetY320Backbone")
