# 2017.12.16 by xiaohang
from __future__ import print_function
from __future__ import division
import numpy as np
import torch
import torch.nn as nn
from torch.nn.parameter import Parameter
from torch.autograd import Variable
from torch.autograd import Function
import torch.nn.functional as F
from collections import OrderedDict
from prototxt import *
import caffe
import caffe.proto.caffe_pb2 as caffe_pb2
# from torch.legacy.nn import SpatialCrossMapLRN as SpatialCrossMapLRNOld
from itertools import product as product


class FCView(nn.Module):
    def __init__(self):
        super(FCView, self).__init__()

    def forward(self, x):
        nB = x.data.size(0)
        x = x.view(nB, -1)
        return x

    def __repr__(self):
        return 'view(nB, -1)'


class Eltwise(nn.Module):
    def __init__(self, operation='+'):
        super(Eltwise, self).__init__()
        self.operation = operation

    def __repr__(self):
        return 'Eltwise %s' % self.operation

    def forward(self, *inputs):
        if self.operation == '+' or self.operation == 'SUM':
            x = inputs[0]
            for i in range(1, len(inputs)):
                x = x + inputs[i]
        elif self.operation == '*' or self.operation == 'MUL':
            x = inputs[0]
            for i in range(1, len(inputs)):
                x = x * inputs[i]
        elif self.operation == '/' or self.operation == 'DIV':
            x = inputs[0]
            for i in range(1, len(inputs)):
                x = x / inputs[i]
        elif self.operation == 'MAX':
            x = inputs[0]
            for i in range(1, len(inputs)):
                x = torch.max(x, inputs[i])
        else:
            print('forward Eltwise, unknown operator')
        return x


class Scale(nn.Module):
    def __init__(self, channels):
        super(Scale, self).__init__()
        self.weight = Parameter(torch.Tensor(channels))
        self.bias = Parameter(torch.Tensor(channels))
        self.channels = channels

    def __repr__(self):
        return 'Scale(channels = %d)' % self.channels

    def forward(self, x):
        nB = x.size(0)
        nC = x.size(1)
        nH = x.size(2)
        nW = x.size(3)
        x = x * self.weight.view(1, nC, 1, 1).expand(nB, nC, nH, nW) + \
            self.bias.view(1, nC, 1, 1).expand(nB, nC, nH, nW)
        return x


class Slice(nn.Module):
    def __init__(self, axis, slice_points):
        super(Slice, self).__init__()
        self.axis = axis
        self.slice_points = slice_points

    def __repr__(self):
        return 'Slice(axis=%d, slice_points=%s)' % (self.axis, self.slice_points)

    def forward(self, x):
        prev = 0
        outputs = []
        is_cuda = x.data.is_cuda
        if is_cuda: device_id = x.data.get_device()
        for idx, slice_point in enumerate(self.slice_points):
            rng = range(prev, slice_point)
            rng = torch.LongTensor(rng)
            if is_cuda: rng = rng.cuda(device_id)
            rng = Variable(rng)
            y = x.index_select(self.axis, rng)
            prev = slice_point
            outputs.append(y)
        return tuple(outputs)


class Concat(nn.Module):
    def __init__(self, axis):
        super(Concat, self).__init__()
        self.axis = axis

    def __repr__(self):
        return 'Concat(axis=%d)' % self.axis

    def forward(self, *inputs):
        return torch.cat(inputs, self.axis)


class Permute(nn.Module):
    def __init__(self, order0, order1, order2, order3):
        super(Permute, self).__init__()
        self.order0 = order0
        self.order1 = order1
        self.order2 = order2
        self.order3 = order3

    def __repr__(self):
        return 'Permute(%d, %d, %d, %d)' % (self.order0, self.order1, self.order2, self.order3)

    def forward(self, x):
        x = x.permute(self.order0, self.order1, self.order2, self.order3).contiguous()
        return x


class Softmax(nn.Module):
    def __init__(self, axis):
        super(Softmax, self).__init__()
        self.axis = axis

    def __repr__(self):
        return 'Softmax(axis=%d)' % self.axis

    def forward(self, x):
        assert (self.axis == len(x.size()) - 1)
        orig_size = x.size()
        dims = x.size(self.axis)
        x = F.softmax(x.view(-1, dims))
        x = x.view(*orig_size)
        return x


class Normalize(nn.Module):
    def __init__(self, n_channels, scale=1.0):
        super(Normalize, self).__init__()
        self.n_channels = n_channels
        self.scale = scale
        self.eps = 1e-10
        self.weight = nn.Parameter(torch.Tensor(self.n_channels))
        self.weight.data *= 0.0
        self.weight.data += self.scale
        self.register_parameter('bias', None)

    def __repr__(self):
        return 'Normalize(channels=%d, scale=%f)' % (self.n_channels, self.scale)

    def forward(self, x):
        norm = x.pow(2).sum(dim=1, keepdim=True).sqrt() + self.eps
        x = x / norm * self.weight.view(1, -1, 1, 1)
        return x


class Flatten(nn.Module):
    def __init__(self, axis):
        super(Flatten, self).__init__()
        self.axis = axis

    def __repr__(self):
        return 'Flatten(axis=%d)' % self.axis

    def forward(self, x):
        left_size = 1
        for i in range(self.axis):
            left_size = x.size(i) * left_size
        return x.view(left_size, -1).contiguous()


# function interface, internal, do not use this one!!!
class LRNFunc(Function):
    def __init__(self, size, alpha=1e-4, beta=0.75, k=1):
        super(LRNFunc, self).__init__()
        self.size = size
        self.alpha = alpha
        self.beta = beta
        self.k = k

    def forward(self, input):
        self.save_for_backward(input)
        self.lrn = SpatialCrossMapLRNOld(self.size, self.alpha, self.beta, self.k)
        self.lrn.type(input.type())
        return self.lrn.forward(input)

    def backward(self, grad_output):
        input, = self.saved_tensors
        return self.lrn.backward(input, grad_output)


# use this one instead
class LRN(nn.Module):
    def __init__(self, size, alpha=1e-4, beta=0.75, k=1):
        super(LRN, self).__init__()
        self.size = size
        self.alpha = alpha
        self.beta = beta
        self.k = k

    def __repr__(self):
        return 'LRN(size=%d, alpha=%f, beta=%f, k=%d)' % (self.size, self.alpha, self.beta, self.k)

    def forward(self, input):
        return LRNFunc(self.size, self.alpha, self.beta, self.k)(input)


class Reshape(nn.Module):
    def __init__(self, dims):
        super(Reshape, self).__init__()
        self.dims = dims

    def __repr__(self):
        return 'Reshape(dims=%s)' % (self.dims)

    def forward(self, x):
        orig_dims = x.size()
        # assert(len(orig_dims) == len(self.dims))
        new_dims = [orig_dims[i] if self.dims[i] == 0 else self.dims[i] for i in range(len(self.dims))]

        return x.view(*new_dims).contiguous()


class PriorBox(nn.Module):
    """Compute priorbox coordinates in center-offset form for each source
    feature map.
    Note:
    This 'layer' has changed between versions of the original SSD
    paper, so we include both versions, but note v2 is the most tested and most
    recent version of the paper.
    """

    def __init__(self, min_size, clip, step, offset, variances):
        super(PriorBox, self).__init__()
        self.min_size = min_size
        self.clip = clip
        self.step = step
        self.offset = offset
        self.variances = variances

    def __repr__(self):
        return 'PriorBox(min_size=%d, clip=%d, step=%d, offset=%f, variances=%s)' % (
        self.min_size, self.clip, self.step, self.offset, self.variances)

    def forward(self, feature, image):
        mean = []
        # assert(feature.size(2) == feature.size(3))
        # assert(image.size(2) == image.size(3))
        feature_height = feature.size(2)
        feature_width = feature.size(3)
        image_height = image.size(2)
        image_width = image.size(3)
        # for i, j in product(range(feature_height), repeat=2):
        for j in range(feature_height):
            for i in range(feature_width):
                # unit center x,y
                cx = (i + self.offset) * self.step / image_width
                cy = (j + self.offset) * self.step / image_height
                ww = float(self.min_size) / image_width
                hh = float(self.min_size) / image_height
                mean += [cx - ww / 2.0, cy - hh / 2.0, cx + ww / 2.0, cy + hh / 2.0]

        # back to torch land
        output1 = torch.Tensor(mean).view(-1, 4)
        output2 = torch.FloatTensor(self.variances).view(1, 4).expand_as(output1)
        if self.clip:
            output1.clamp_(max=1, min=0)
        output1 = output1.view(1, 1, -1)
        output2 = output2.contiguous().view(1, 1, -1)
        output = torch.cat([output1, output2], 1)
        if feature.data.is_cuda:
            device_id = feature.data.get_device()
            return Variable(output.cuda(device_id))
        else:
            return Variable(output)


class CaffeNet(nn.Module):
    def __init__(self, protofile, width=None, height=None):
        super(CaffeNet, self).__init__()
        self.net_info = parse_prototxt(protofile)
        self.models = self.create_network(self.net_info, width, height)
        for name, model in self.models.items():
            self.add_module(name, model)

        self.has_mean = False
        if 'mean_file' in self.net_info['props']:
            self.has_mean = True
            self.mean_file = self.net_info['props']['mean_file']

        self.blobs = None

    def set_mean_file(self, mean_file):
        if mean_file != "":
            self.has_mean = True
            self.mean_file = mean_file

        else:
            self.has_mean = False
            self.mean_file = ""

    def get_outputs(self, output_names):
        outputs = []
        for name in output_names:
            outputs.append(self.blobs[name])
        return outputs

    def forward(self, data):
        if self.has_mean:
            nB = data.data.size(0)
            nC = data.data.size(1)
            nH = data.data.size(2)
            nW = data.data.size(3)
            data = data - Variable(self.mean_img.view(1, nC, nH, nW).expand(nB, nC, nH, nW))

        self.blobs = OrderedDict()
        self.blobs['data'] = data

        layers = self.net_info['layers']
        layer_num = len(layers)
        i = 0
        while i < layer_num:
            layer = layers[i]
            lname = layer['name']
            ltype = layer['type']
            tname = layer['top']
            bname = layer['bottom']
            bnames = bname if type(bname) == list else [bname]
            tnames = tname if type(tname) == list else [tname]

            if ltype in ['Data', 'Accuracy', 'SoftmaxWithLoss', 'Region']:
                i = i + 1
            else:
                bdatas = [self.blobs[name] for name in bnames]
                tdatas = self._modules[lname](*bdatas)
                if type(tdatas) != tuple:
                    tdatas = (tdatas,)

                assert (len(tdatas) == len(tnames))
                for index, tdata in enumerate(tdatas):
                    self.blobs[tnames[index]] = tdata
                i = i + 1
            input_size = self.blobs[bnames[0]].size()
            output_size = self.blobs[tnames[0]].size()
            print('forward %-30s %s -> %s' % (lname, list(input_size), list(output_size)))

        return self.blobs

    #        if type(self.outputs) == list:
    #            odatas = [blobs[name] for name in self.outputs]
    #            return odatas
    #        else:
    #            return blobs[self.outputs]

    def print_network(self):
        print(self)
        print_prototxt(self.net_info)

    def load_weights(self, caffemodel):
        if self.has_mean:
            print('mean_file', self.mean_file)
            mean_blob = caffe_pb2.BlobProto()
            mean_blob.ParseFromString(open(self.mean_file, 'rb').read())

            if 'input_shape' in self.net_info['props']:
                channels = int(self.net_info['props']['input_shape']['dim'][1])
                height = int(self.net_info['props']['input_shape']['dim'][2])
                width = int(self.net_info['props']['input_shape']['dim'][3])
            else:
                channels = int(self.net_info['props']['input_dim'][1])
                height = int(self.net_info['props']['input_dim'][2])
                width = int(self.net_info['props']['input_dim'][3])

            mu = np.array(mean_blob.data)
            mu.resize(channels, height, width)
            mu = mu.mean(1).mean(1)
            mean_img = torch.from_numpy(mu).view(channels, 1, 1).expand(channels, height, width).float()

            self.register_buffer('mean_img', torch.zeros(channels, height, width))
            self.mean_img.copy_(mean_img)

        model = parse_caffemodel(caffemodel)
        layers = model.layer
        if len(layers) == 0:
            print('Using V1LayerParameter')
            layers = model.layers

        lmap = {}
        for l in layers:
            lmap[l.name] = l

        layers = self.net_info['layers']
        layer_num = len(layers)
        i = 0
        while i < layer_num:
            layer = layers[i]
            lname = layer['name']
            ltype = layer['type']
            if ltype == 'CONVOLUTION':
                print('load weights %s' % lname)
                convolution_param = layer['convolution_param']
                bias = True
                if 'bias_term' in convolution_param and convolution_param['bias_term'] == 'false':
                    bias = False
                # weight_blob = lmap[lname].blobs[0]
                # print('caffe weight shape', weight_blob.num, weight_blob.channels, weight_blob.height, weight_blob.width)
                caffe_weight = np.array(lmap[lname].blobs[0].data)
                caffe_weight = torch.from_numpy(caffe_weight).view_as(self.models[lname].weight)
                self.models[lname].weight.data.copy_(caffe_weight)
                if bias and len(lmap[lname].blobs) > 1:
                    self.models[lname].bias.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[1].data)))
                    # print("convlution %s has bias" % lname)
                i = i + 1
            elif ltype == 'BatchNorm':
                print('load weights %s' % lname)
                self.models[lname].running_mean.copy_(
                    torch.from_numpy(np.array(lmap[lname].blobs[0].data) / lmap[lname].blobs[2].data[0]))
                self.models[lname].running_var.copy_(
                    torch.from_numpy(np.array(lmap[lname].blobs[1].data) / lmap[lname].blobs[2].data[0]))
                i = i + 1
            elif ltype == 'Scale':
                print('load weights %s' % lname)
                self.models[lname].weight.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[0].data)))
                self.models[lname].bias.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[1].data)))
                i = i + 1
            elif ltype == 'Normalize':
                print('load weights %s' % lname)
                self.models[lname].weight.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[0].data)))
                i = i + 1
            elif ltype == 'InnerProduct':
                print('load weights %s' % lname)
                if type(self.models[lname]) == nn.Sequential:
                    self.models[lname][1].weight.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[0].data)))
                    if len(lmap[lname].blobs) > 1:
                        self.models[lname][1].bias.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[1].data)))
                else:
                    self.models[lname].weight.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[0].data)))
                    if len(lmap[lname].blobs) > 1:
                        self.models[lname].bias.data.copy_(torch.from_numpy(np.array(lmap[lname].blobs[1].data)))
                i = i + 1
            elif ltype in ['Pooling', 'Eltwise', 'RELU', 'Region', 'Permute', 'Flatten', 'Slice', 'Concat', 'Softmax',
                           'SoftmaxWithLoss', 'LRN', 'Dropout', 'Reshape', 'PriorBox']:
                i = i + 1
            else:
                print('load_weights: unknown type %s' % ltype)
                i = i + 1

    def create_network(self, net_info, input_width=None, input_height=None):
        models = OrderedDict()
        blob_channels = dict()
        blob_width = dict()
        blob_height = dict()

        layers = net_info['layers']
        props = net_info['props']
        layer_num = len(layers)

        if 'input_shape' in props:
            blob_channels['data'] = int(props['input_shape']['dim'][1])
            blob_height['data'] = int(props['input_shape']['dim'][2])
            blob_width['data'] = int(props['input_shape']['dim'][3])

            self.width = int(props['input_shape']['dim'][3])
            self.height = int(props['input_shape']['dim'][2])
        else:
            blob_channels['data'] = int(props['input_dim'][1])
            blob_height['data'] = int(props['input_dim'][2])
            blob_width['data'] = int(props['input_dim'][3])

            self.width = int(props['input_dim'][3])
            self.height = int(props['input_dim'][2])

        if input_width != None and input_height != None:
            blob_width['data'] = input_width
            blob_height['data'] = input_height
            self.width = input_width
            self.height = input_height

        i = 0
        while i < layer_num:
            layer = layers[i]
            lname = layer['name']
            ltype = layer['type']
            if ltype == 'Data':
                i = i + 1
                continue
            bname = layer['bottom']
            tname = layer['top']
            if ltype == 'CONVOLUTION':
                convolution_param = layer['convolution_param']
                channels = blob_channels[bname]
                out_filters = int(convolution_param['num_output'])
                kernel_size = int(convolution_param['kernel_size'])
                stride = int(convolution_param['stride']) if 'stride' in convolution_param else 1
                pad = int(convolution_param['pad']) if 'pad' in convolution_param else 0
                dilation = 1  # Set to default!
                group = int(convolution_param['group']) if 'group' in convolution_param else 1
                bias = True
                if 'bias_term' in convolution_param and convolution_param['bias_term'] == 'false':
                    bias = False
                models[lname] = nn.Conv2d(channels, out_filters, kernel_size, stride, pad, dilation, group, bias=bias)
                blob_channels[tname] = out_filters
                blob_width[tname] = (blob_width[bname] + 2 * pad - kernel_size) // stride + 1
                blob_height[tname] = (blob_height[bname] + 2 * pad - kernel_size) // stride + 1
                i = i + 1
            elif ltype == 'BatchNorm':
                momentum = 0.9
                if 'batch_norm_param' in layer and 'moving_average_fraction' in layer['batch_norm_param']:
                    momentum = float(layer['batch_norm_param']['moving_average_fraction'])
                channels = blob_channels[bname]
                models[lname] = nn.BatchNorm2d(channels, momentum=momentum, affine=False)
                blob_channels[tname] = channels
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'Scale':
                channels = blob_channels[bname]
                models[lname] = Scale(channels)
                blob_channels[tname] = channels
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'RELU':
                inplace = (bname == tname)
                if 'relu_param' in layer and 'negative_slope' in layer['relu_param']:
                    negative_slope = float(layer['relu_param']['negative_slope'])
                    models[lname] = nn.LeakyReLU(negative_slope=negative_slope, inplace=inplace)
                else:
                    models[lname] = nn.ReLU(inplace=inplace)
                blob_channels[tname] = blob_channels[bname]
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'Pooling':
                kernel_size = int(layer['pooling_param']['kernel_size'])
                stride = int(layer['pooling_param']['stride'])
                padding = 0
                if 'pad' in layer['pooling_param']:
                    padding = int(layer['pooling_param']['pad'])
                pool_type = layer['pooling_param']['pool']
                if pool_type == 'MAX':
                    models[lname] = nn.MaxPool2d(kernel_size, stride, padding=padding, ceil_mode=True)
                elif pool_type == 'AVE':
                    models[lname] = nn.AvgPool2d(kernel_size, stride, padding=padding, ceil_mode=True)

                if stride > 1:
                    blob_width[tname] = (blob_width[bname] + 2 * padding - kernel_size + 1) // stride + 1
                    blob_height[tname] = (blob_height[bname] + 2 * padding - kernel_size + 1) // stride + 1
                else:
                    blob_width[tname] = blob_width[bname] + 2 * padding - kernel_size + 1
                    blob_height[tname] = blob_height[bname] + 2 * padding - kernel_size + 1
                blob_channels[tname] = blob_channels[bname]
                i = i + 1
            elif ltype == 'Eltwise':
                operation = 'SUM'
                if 'eltwise_param' in layer and 'operation' in layer['eltwise_param']:
                    operation = layer['eltwise_param']['operation']
                bname0 = bname[0]
                bname1 = bname[1]
                models[lname] = Eltwise(operation)
                blob_channels[tname] = blob_channels[bname0]
                blob_width[tname] = blob_width[bname0]
                blob_height[tname] = blob_height[bname0]
                i = i + 1
            elif ltype == 'InnerProduct':
                filters = int(layer['inner_product_param']['num_output'])
                if blob_width[bname] != -1 or blob_height[bname] != -1:
                    channels = int(blob_channels[bname] * blob_width[bname] * blob_height[bname])
                    models[lname] = nn.Sequential(FCView(), nn.Linear(channels, filters))
                else:
                    channels = blob_channels[bname]
                    models[lname] = nn.Linear(channels, filters)
                blob_channels[tname] = filters
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            elif ltype == 'Dropout':
                channels = blob_channels[bname]
                dropout_ratio = float(layer['dropout_param']['dropout_ratio'])
                models[lname] = nn.Dropout(dropout_ratio, inplace=True)
                blob_channels[tname] = blob_channels[bname]
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'Normalize':
                channels = blob_channels[bname]
                scale = float(layer['norm_param']['scale_filler']['value'])
                models[lname] = Normalize(channels, scale)
                blob_channels[tname] = blob_channels[bname]
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'LRN':
                local_size = int(layer['lrn_param']['local_size'])
                alpha = float(layer['lrn_param']['alpha'])
                beta = float(layer['lrn_param']['beta'])
                models[lname] = LRN(local_size, alpha, beta)
                blob_channels[tname] = blob_channels[bname]
                blob_width[tname] = blob_width[bname]
                blob_height[tname] = blob_height[bname]
                i = i + 1
            elif ltype == 'Permute':
                orders = layer['permute_param']['order']
                order0 = int(orders[0])
                order1 = int(orders[1])
                order2 = int(orders[2])
                order3 = int(orders[3])
                models[lname] = Permute(order0, order1, order2, order3)
                shape = [1, blob_channels[bname], blob_height[bname], blob_width[bname]]
                blob_channels[tname] = shape[order1]
                blob_height[tname] = shape[order2]
                blob_width[tname] = shape[order3]
                i = i + 1
            elif ltype == 'Flatten':
                axis = int(layer['flatten_param']['axis'])
                models[lname] = Flatten(axis)
                blob_channels[tname] = blob_channels[bname] * blob_width[bname] * blob_height[bname]
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            elif ltype == 'Slice':
                axis = int(layer['slice_param']['axis'])
                assert (axis == 1)
                assert (type(tname) == list)
                slice_points = layer['slice_param']['slice_point']
                assert (type(slice_points) == list)
                assert (len(slice_points) == len(tname) - 1)
                slice_points = [int(s) for s in slice_points]
                shape = [1, blob_channels[bname], blob_height[bname], blob_width[bname]]
                slice_points.append(shape[axis])
                models[lname] = Slice(axis, slice_points)
                prev = 0
                for idx, tn in enumerate(tname):
                    blob_channels[tn] = slice_points[idx] - prev
                    blob_width[tn] = blob_width[bname]
                    blob_height[tn] = blob_height[bname]
                    prev = slice_points[idx]
                i = i + 1
            elif ltype == 'Concat':
                axis = 1
                if 'concat_param' in layer and 'axis' in layer['concat_param']:
                    axis = int(layer['concat_param']['axis'])
                models[lname] = Concat(axis)
                if axis == 1:
                    blob_channels[tname] = 0
                    for bn in bname:
                        blob_channels[tname] += blob_channels[bn]
                        blob_width[tname] = blob_width[bn]
                        blob_height[tname] = blob_height[bn]
                elif axis == 2:
                    blob_channels[tname] = blob_channels[bname[0]]
                    blob_width[tname] = 1
                    blob_height[tname] = 0
                    for bn in bname:
                        blob_height[tname] += blob_height[bn]
                i = i + 1
            elif ltype == 'PriorBox':
                min_size = int(layer['prior_box_param']['min_size'])
                clip = (layer['prior_box_param']['clip'] == 'true')
                step = int(layer['prior_box_param']['step'])
                offset = float(layer['prior_box_param']['offset'])
                variances = layer['prior_box_param']['variance']
                variances = [float(v) for v in variances]
                models[lname] = PriorBox(min_size, clip, step, offset, variances)
                blob_channels[tname] = 1
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            elif ltype == 'Reshape':
                reshape_dims = layer['reshape_param']['shape']['dim']
                reshape_dims = [int(item) for item in reshape_dims]
                models[lname] = Reshape(reshape_dims)
                blob_channels[tname] = 1
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            elif ltype == 'Softmax':
                axis = 1
                if 'softmax_param' in layer and 'axis' in layer['softmax_param']:
                    axis = int(layer['softmax_param']['axis'])
                models[lname] = Softmax(axis)
                blob_channels[tname] = blob_channels[bname]
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            elif ltype == 'SoftmaxWithLoss':
                models[lname] = nn.CrossEntropyLoss()
                blob_channels[tname] = 1
                blob_width[tname] = 1
                blob_height[tname] = 1
                i = i + 1
            else:
                print('create_network: unknown type #%s#' % ltype)
                i = i + 1
            input_width = blob_width[bname] if type(bname) != list else blob_width[bname[0]]
            input_height = blob_height[bname] if type(bname) != list else blob_height[bname[0]]
            output_width = blob_width[tname] if type(tname) != list else blob_width[tname[0]]
            output_height = blob_height[tname] if type(tname) != list else blob_height[tname[0]]
            print('create %-30s (%4d x %4d) -> (%4d x %4d)' % (
            lname, input_width, input_height, output_width, output_height))

        return models
