import torch
import torch.nn as nn

from waveunet_utils import crop, Resample1d, ConvLayer

class UpsamplingBlock(nn.Module):
    def __init__(self, n_inputs, n_shortcut, n_outputs, kernel_size, stride, depth, conv_type, res):
        super(UpsamplingBlock, self).__init__()
        assert(stride > 1)

        # n_shortcut == n_outputs

        # CONV 1 for UPSAMPLING
        if res == "fixed":
            self.upconv = Resample1d(n_inputs, 15, stride, transpose=True)
        else:
            self.upconv = ConvLayer(n_inputs, n_inputs, kernel_size, stride, conv_type, transpose=True)


        # CONVS to combine high- with low-level information (from shortcut)
        self.post_shortcut_convs = nn.ModuleList([ConvLayer(n_inputs + n_shortcut, n_outputs, kernel_size, 1, conv_type)] +
                                                 [ConvLayer(n_outputs, n_outputs, kernel_size, 1, conv_type) for _ in range(depth - 1)])

    def forward(self, x, shortcut):
        # UPSAMPLE HIGH-LEVEL FEATURES
        upsampled = self.upconv(x)

        # Prepare shortcut connection
        combined = crop(shortcut, upsampled)

        # Combine high- and low-level features
        for conv in self.post_shortcut_convs:
            combined = conv(torch.cat([combined, crop(upsampled, combined)], dim=1))
        return combined

    def get_output_size(self, input_size):
        curr_size = self.upconv.get_output_size(input_size)

        # Combine convolutions
        for conv in self.post_shortcut_convs:
            curr_size = conv.get_output_size(curr_size)

        return curr_size

class DownsamplingBlock(nn.Module):
    def __init__(self, n_inputs, n_shortcut, n_outputs, kernel_size, stride, depth, conv_type, res):
        super(DownsamplingBlock, self).__init__()
        assert(stride > 1)

        # n_shortcut == n_outputs

        self.kernel_size = kernel_size
        self.stride = stride

        # CONV 1
        self.pre_shortcut_convs = nn.ModuleList([ConvLayer(n_inputs, n_outputs, kernel_size, 1, conv_type)] +
                                                [ConvLayer(n_outputs, n_outputs, kernel_size, 1, conv_type) for _ in range(depth - 1)])


        # CONV 2 with decimation
        if res == "fixed":
            self.downconv = Resample1d(n_outputs, 15, stride) # Resampling with fixed-size sinc lowpass filter
        else:
            self.downconv = ConvLayer(1, 29, kernel_size, stride, conv_type)

    def forward(self, x):
        # PREPARING SHORTCUT FEATURES
        shortcut = x
        # print(f'here: {shortcut}')
        for conv in self.pre_shortcut_convs:
            # print(f'conv: {conv}')
            shortcut = conv(shortcut)
            # print(f'after: {shortcut}')

        # PREPARING FOR DOWNSAMPLING
        out = shortcut
        print("====="*10)
        # print(out)
        print("====="*10)
        # DOWNSAMPLING
        out = self.downconv(out)
        # print("+"*10)
        return out, shortcut

    def get_input_size(self, output_size):
        curr_size = self.downconv.get_input_size(output_size)

        for conv in reversed(self.pre_shortcut_convs):
            curr_size = conv.get_input_size(curr_size)
        return curr_size

class WaveunetLyrics(nn.Module):
    def __init__(self, num_inputs, num_channels, num_outputs, kernel_size, input_sample, output_sample, conv_type, res, depth=1, strides=2):
        '''

        :param num_inputs:              number of input channel (1)
        :param num_channels:            channels of conv layers in each block
        :param num_outputs:             number of output channel (29: {a, b, ..., z, ',  , eps})
        :param kernel_size:             kernel size for conv layers [f_D, f_U]
        :param input_size:              number of input samples
        :param conv_type:               normalize type
        :param res:                     resampling strategy
        :param depth:                   conv depth (default 1)
        :param strides:                 conv strides (default 2)
        '''

        super(WaveunetLyrics, self).__init__()
        print("in duoc ne")
        self.down_levels = len(num_channels[0])-1
        self.up_levels = len(num_channels[1])
        self.strides = strides
        self.kernel_size = kernel_size
        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.depth = depth

        # Only odd filter kernels allowed
        assert(kernel_size[0] % 2 == 1 and kernel_size[1] % 2 == 1)

        module = nn.Module()

        module.downsampling_blocks = nn.ModuleList()
        module.upsampling_blocks = nn.ModuleList()

        for i in range(self.down_levels):
            in_ch = 1 if i == 0 else num_channels[0][i-1]

            module.downsampling_blocks.append(
                DownsamplingBlock(in_ch, None, num_channels[0][i], kernel_size[0], strides, depth, conv_type, res))

        for i in range(self.up_levels):
            module.upsampling_blocks.append(
                UpsamplingBlock(num_channels[0][-1-i], num_channels[0][-2-i], num_channels[0][-2-i], kernel_size[1], strides, depth, conv_type, res))

        module.bottlenecks = nn.ModuleList(
            [ConvLayer(num_channels[0][-2], num_channels[0][-1], kernel_size[0], 1, conv_type) for _ in range(depth)])

        # Output conv : outputs a probability matrix P
        outputs = self.num_outputs
        module.output_conv = nn.Conv1d(num_channels[0][-3], outputs, 1)

        self.waveunets = module

        self.set_output_size(input_sample, output_sample)

    def set_output_size(self, input_sample, output_sample):

        self.input_size, self.output_size = (input_sample, output_sample) # self.check_padding(220)
        print("Using valid convolutions with " + str(self.input_size) + " inputs and " + str(self.output_size) + " outputs")

        assert((self.input_size - self.output_size) % 2 == 0)
        self.shapes = {"output_start_frame" : (self.input_size - self.output_size) // 2,
                       "output_end_frame" : (self.input_size - self.output_size) // 2 + self.output_size,
                       "output_frames" : self.output_size,
                       "input_frames" : self.input_size}

    def forward_module(self, x, module):
        '''
        A forward pass through a single Wave-U-Net (multiple Wave-U-Nets might be used, one for each source)
        :param x: Input mix
        :param module: Network module to be used for prediction
        :return: Source estimates
        '''
        shortcuts = []
        out = x

        # print(x.shape)

        # DOWNSAMPLING BLOCKS
        for block in module.downsampling_blocks:
            print("herreeeee")
            # print(block)
            out, short = block(out)
            print(out.shape, short.shape)
            shortcuts.append(short)

        print("after downsampling blocks:", out.shape, len(shortcuts))

        # BOTTLENECK CONVOLUTION
        for conv in module.bottlenecks:
            out = conv(out)
            print(out.shape)

        print("after bottleneck convolution:", out.shape)

        # UPSAMPLING BLOCKS
        for idx, block in enumerate(module.upsampling_blocks):
            out = block(out, shortcuts[-1 - idx])
            print(out.shape)

        print("after upsampling blocks:", out.shape)

        # OUTPUT CONV
        out = module.output_conv(out)

        print("after output conv:", out.shape)

        # if not self.training:  # At test time clip predictions to valid amplitude range
        #     out = out.clamp(min=-1.0, max=1.0)

        # log_softmax
        out = nn.functional.log_softmax(out, 1)

        return out

    def forward(self, x):
        curr_input_size = x.shape[-1]
        # print(curr_input_size, self.input_size)
        assert(curr_input_size == self.input_size) # User promises to feed the proper input himself, to get the pre-calculated (NOT the originally desired) output size

        return self.forward_module(x, self.waveunets)

class Waveunet(nn.Module):
    def __init__(self, num_inputs, num_channels, num_outputs, instruments, kernel_size, target_output_size, conv_type, res, separate=False, depth=1, strides=2):
        super(Waveunet, self).__init__()

        self.num_levels = len(num_channels)
        self.strides = strides
        self.kernel_size = kernel_size
        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.depth = depth
        self.instruments = instruments
        self.separate = separate

        # Only odd filter kernels allowed
        assert(kernel_size % 2 == 1)

        self.waveunets = nn.ModuleDict()

        model_list = instruments if separate else ["ALL"]
        # Create a model for each source if we separate sources separately, otherwise only one (model_list=["ALL"])
        for instrument in model_list:
            module = nn.Module()

            module.downsampling_blocks = nn.ModuleList()
            module.upsampling_blocks = nn.ModuleList()

            for i in range(self.num_levels - 1):
                in_ch = num_inputs if i == 0 else num_channels[i]

                module.downsampling_blocks.append(
                    DownsamplingBlock(in_ch, num_channels[i], num_channels[i+1], kernel_size, strides, depth, conv_type, res))

            for i in range(0, self.num_levels - 1):
                module.upsampling_blocks.append(
                    UpsamplingBlock(num_channels[-1-i], num_channels[-2-i], num_channels[-2-i], kernel_size, strides, depth, conv_type, res))

            module.bottlenecks = nn.ModuleList(
                [ConvLayer(num_channels[-1], num_channels[-1], kernel_size, 1, conv_type) for _ in range(depth)])

            # Output conv
            outputs = num_outputs if separate else num_outputs * len(instruments)
            module.output_conv = nn.Conv1d(num_channels[0], outputs, 1)

            self.waveunets[instrument] = module

        self.set_output_size(target_output_size)

    def set_output_size(self, target_output_size):
        self.target_output_size = target_output_size

        self.input_size, self.output_size = self.check_padding(target_output_size)
        print("Using valid convolutions with " + str(self.input_size) + " inputs and " + str(self.output_size) + " outputs")

        assert((self.input_size - self.output_size) % 2 == 0)
        self.shapes = {"output_start_frame" : (self.input_size - self.output_size) // 2,
                       "output_end_frame" : (self.input_size - self.output_size) // 2 + self.output_size,
                       "output_frames" : self.output_size,
                       "input_frames" : self.input_size}

    def check_padding(self, target_output_size):
        # Ensure number of outputs covers a whole number of cycles so each output in the cycle is weighted equally during training
        bottleneck = 1

        while True:
            out = self.check_padding_for_bottleneck(bottleneck, target_output_size)
            if out is not False:
                return out
            bottleneck += 1

    def check_padding_for_bottleneck(self, bottleneck, target_output_size):
        module = self.waveunets[[k for k in self.waveunets.keys()][0]]
        try:
            curr_size = bottleneck
            for idx, block in enumerate(module.upsampling_blocks):
                curr_size = block.get_output_size(curr_size)
            output_size = curr_size

            # Bottleneck-Conv
            curr_size = bottleneck
            for block in reversed(module.bottlenecks):
                curr_size = block.get_input_size(curr_size)
            for idx, block in enumerate(reversed(module.downsampling_blocks)):
                curr_size = block.get_input_size(curr_size)

            assert(output_size >= target_output_size)
            return curr_size, output_size
        except AssertionError as e:
            return False

    def forward_module(self, x, module):
        '''
        A forward pass through a single Wave-U-Net (multiple Wave-U-Nets might be used, one for each source)
        :param x: Input mix
        :param module: Network module to be used for prediction
        :return: Source estimates
        '''
        shortcuts = []
        out = x

        # DOWNSAMPLING BLOCKS
        for block in module.downsampling_blocks:
            out, short = block(out)
            shortcuts.append(short)

        # BOTTLENECK CONVOLUTION
        for conv in module.bottlenecks:
            out = conv(out)

        # UPSAMPLING BLOCKS
        for idx, block in enumerate(module.upsampling_blocks):
            out = block(out, shortcuts[-1 - idx])

        # OUTPUT CONV
        out = module.output_conv(out)
        if not self.training:  # At test time clip predictions to valid amplitude range
            out = out.clamp(min=-1.0, max=1.0)
        return out

    def forward(self, x, inst=None):
        curr_input_size = x.shape[-1]
        assert(curr_input_size == self.input_size) # User promises to feed the proper input himself, to get the pre-calculated (NOT the originally desired) output size

        if self.separate:
            return {inst : self.forward_module(x, self.waveunets[inst])}
        else:
            assert(len(self.waveunets) == 1)
            out = self.forward_module(x, self.waveunets["ALL"])

            out_dict = {}
            for idx, inst in enumerate(self.instruments):
                out_dict[inst] = out[:, idx * self.num_outputs:(idx + 1) * self.num_outputs]
            return out_dict