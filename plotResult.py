import os
import torch
import torchvision
import torch.utils.data as data
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torch.optim as optim
import torchvision
from torchvision import datasets, transforms
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from preTrainChannelGroupNet import CGNN
from CUB_loader import CUB200_loader
import logging
def get_log(file_name):
    logger = logging.getLogger('train')  # 设定logger的名字
    logger.setLevel(logging.INFO)  # 设定logger得等级

    ch = logging.StreamHandler() # 输出流的hander，用与设定logger的各种信息
    ch.setLevel(logging.INFO)  # 设定输出hander的level

    fh = logging.FileHandler(file_name, mode='a')  # 文件流的hander，输出得文件名称，以及mode设置为覆盖模式
    fh.setLevel(logging.INFO)  # 设定文件hander得lever



    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)  # 两个hander设置个是，输出得信息包括，时间，信息得等级，以及message
    fh.setFormatter(formatter)
    logger.addHandler(fh)  # 将两个hander添加到我们声明的logger中去
    logger.addHandler(ch)
    return logger

logger=get_log('test.log')

resultGradImg=None

def normalize(I):
    # 归一化梯度map，先归一化到 mean=0 std=1
    norm = (I - I.mean()) / I.std()
    # 把 std 重置为 0.1，让梯度map中的数值尽可能接近 0
    norm = norm * 0.1
    # 均值加 0.5，保证大部分的梯度值为正
    norm = norm + 0.5
    # 把 0，1 以外的梯度值分别设置为 0 和 1
    norm = norm.clip(0, 1)
    return norm

torch.manual_seed(1)
torch.cuda.manual_seed_all(1)

#visualize tensor (3,H,W)
def visualize(X):
    X=normalize(X.permute(1, 2, 0).cpu().detach().numpy())
    plt.imshow(X)
    plt.show()


class GTBNN(torch.nn.Module):
    """B-CNN for CUB200.
    The B-CNN model is illustrated as follows.
    conv1^2 (64) -> pool1 -> conv2^2 (128) -> pool2 -> conv3^3 (256) -> pool3
    -> conv4^3 (512) -> pool4 -> conv5^3 (512) -> bilinear pooling
    -> sqrt-normalize -> L2-normalize -> fc (200).
    The network accepts a 3*448*448 input, and the pool5 activation has shape
    512*28*28 since we down-sample 5 times.
    Attributes:
        features, torch.nn.Module: Convolution and pooling layers.
        fc, torch.nn.Module: 200.
    """
    def __init__(self):
        """Declare all needed layers."""
        torch.nn.Module.__init__(self)
        ######################### Convolution and pooling layers of VGG-16.
        self.features = torchvision.models.vgg16(pretrained=True).features  # fine tune?
        self.features = torch.nn.Sequential(*list(self.features.children())
        [:-22])  # Remove pool2 and rest, lack of computational resource
        # No grad for convVGG
        # for param in self.features.parameters():
        #     param.requires_grad = False

        #################### Channel Grouping Net
        self.fc1_ = torch.nn.Linear(128*28*28, 64)#lack of resource
        self.fc2_ = torch.nn.Linear(128*28*28, 64)
        self.fc3_ = torch.nn.Linear(128*28*28, 64)

        self.fc1 = torch.nn.Linear(64, 128)
        self.fc2 = torch.nn.Linear(64, 128)
        self.fc3 = torch.nn.Linear(64, 128)

        self.layerNorm = nn.LayerNorm([448, 448])
        # global grad for hook
        self.image_reconstruction = None
        self.register_hooks()

        ################### STN input N*3*448*448
        self.localization = [
                nn.Sequential(
                nn.MaxPool2d(4,stride=4),#112
                nn.ReLU(True),

                nn.Conv2d(3, 32, kernel_size=5,stride=1,padding=2),  # 112
                nn.MaxPool2d(2, stride=2),  # 56
                nn.ReLU(True),

                nn.Conv2d(32, 48, kernel_size=3,stride=1,padding=1),
                nn.MaxPool2d(2, stride=2),  # 56/2=28
                nn.ReLU(True),

                nn.Conv2d(48, 64, kernel_size=3, stride=1, padding=1),
                nn.MaxPool2d(2, stride=2),  # 28/2=14
                nn.ReLU(True) #output 64*14*14
            ).cuda(),
            nn.Sequential(
                nn.MaxPool2d(4, stride=4),  # 112
                nn.ReLU(True),

                nn.Conv2d(3, 32, kernel_size=5, stride=1, padding=2),  # 112
                nn.MaxPool2d(2, stride=2),  # 56
                nn.ReLU(True),

                nn.Conv2d(32, 48, kernel_size=3, stride=1, padding=1),
                nn.MaxPool2d(2, stride=2),  # 56/2=28
                nn.ReLU(True),

                nn.Conv2d(48, 64, kernel_size=3, stride=1, padding=1),
                nn.MaxPool2d(2, stride=2),  # 28/2=14
                nn.ReLU(True)  # output 64*14*14
            ).cuda(),
            nn.Sequential(
                nn.MaxPool2d(4, stride=4),  # 112
                nn.ReLU(True),

                nn.Conv2d(3, 32, kernel_size=5, stride=1, padding=2),  # 112
                nn.MaxPool2d(2, stride=2),  # 56
                nn.ReLU(True),

                nn.Conv2d(32, 48, kernel_size=3, stride=1, padding=1),
                nn.MaxPool2d(2, stride=2),  # 56/2=28
                nn.ReLU(True),

                nn.Conv2d(48, 64, kernel_size=3, stride=1, padding=1),
                nn.MaxPool2d(2, stride=2),  # 28/2=14
                nn.ReLU(True)  # output 64*14*14
            ).cuda()
        ]
        # Regressor for the 3 * 2 affine matrix
        self.fc_loc = [
                nn.Sequential(
                nn.Linear(64 * 14 * 14, 32),
                nn.ReLU(True),
                nn.Linear(32, 3 * 2)
            ).cuda(),
            nn.Sequential(
                nn.Linear(64 * 14 * 14, 32),
                nn.ReLU(True),
                nn.Linear(32, 3 * 2)
            ).cuda(),
            nn.Sequential(
                nn.Linear(64 * 14 * 14, 32),
                nn.ReLU(True),
                nn.Linear(32, 3 * 2)
            ).cuda()
        ]
        # Initialize the weights/bias with identity transformation
        for fc_locx in self.fc_loc:
            fc_locx[2].weight.data.zero_()
            fc_locx[2].bias.data.copy_(torch.tensor([1, 0, 0, 0, 1, 0], dtype=torch.float))

        ########################Bilinear CNN output 256 channels
        self.bcnnConv=torch.nn.Sequential(*list(torchvision.models.vgg16(pretrained=True).features.children())
                                            [:-15])  # Remove pool3 and rest.
        #BCNN Linear classifier.
        self.fc = torch.nn.Linear(256 ** 2, 200)
        torch.nn.init.kaiming_normal(self.fc.weight.data)  # 何凯明初始化
        if self.fc.bias is not None:
            torch.nn.init.constant(self.fc.bias.data, val=0)  # fc层的bias进行constant初始化

    def register_hooks(self):
        def first_layer_hook_fn(module, grad_in, grad_out):
            # 在全局变量中保存输入图片的梯度，该梯度由第一层卷积层
            # 反向传播得到，因此该函数需绑定第一个 Conv2d Layer
            self.image_reconstruction = grad_in[0]

        # 获取 module，
        modules = list(self.features.named_children())

        # # 遍历所有 module，对 ReLU 注册 forward hook 和 backward hook
        # for name, module in modules:
        #     if isinstance(module, nn.ReLU):
        #         module.register_forward_hook(forward_hook_fn)
        #         module.register_backward_hook(backward_hook_fn)

        # 对第1层卷积层注册 hook
        first_layer = modules[0][1]
        first_layer.register_backward_hook(first_layer_hook_fn)

    def weightByGrad(self, Xi,i,Xo):
        XiSum=torch.sum(Xi,dim=1)
        XiSum.backward(torch.ones(XiSum.shape).cuda(),retain_graph=True)
        #normalize, not tried......
        gradImg= self.image_reconstruction.data#[0]#.permute(1, 2, 0)#0 for only one image
        gradImg = torch.sqrt(gradImg * gradImg)  # needed
        gradImg = self.layerNorm(gradImg)
        # global resultGradImg
        # resultGradImg=normalize(gradImg.permute(1, 2, 0).cpu().numpy())
        # plt.imshow(resultGradImg)
        # plt.show()
        res=gradImg*0.5*Xo+Xo
        # print(res.size(),flush=True)
        # res=normalize(res.squveeze(dim=0).permute(1, 2, 0).cpu().detach().numpy())
        # plt.imsave('gradImgs'+str(i)+'.jpg',res)

        self.zero_grad()
        visualize(res[0])

        return res

    # Spatial transformer network forward function
    def stn(self, x, i):
        xs = self.localization[i](x)
        xs = xs.view(-1, 64 * 14 * 14)
        theta = self.fc_loc[i](xs)
        theta = theta.view(-1, 2, 3)

        grid = F.affine_grid(theta, torch.Size([x.size()[0], x.size()[1], 96, 96]))  # x.size())
        x = F.grid_sample(x, grid)

        visualize(x[0])

        return x

    def forward(self, Xo):
        """Forward pass of the network.
        Args:
            X, torch.autograd.Variable of shape N*3*448*448.
        Returns:
            Score, torch.autograd.Variable of shape N*200.
        """
        N = Xo.size()[0]
        assert Xo.size() == (N, 3, 448, 448)
        X = self.features(Xo)
        assert X.size() == (N, 128, 224, 224)
        Xp = nn.MaxPool2d(kernel_size=8, stride=8)(X)
        Xp = Xp.view(-1, 128 * 28 * 28)
        X1 = F.relu(self.fc1_(Xp))
        X2 = F.relu(self.fc2_(Xp))
        X3 = F.relu(self.fc3_(Xp))
        X1 = self.fc1(X1)
        X2 = self.fc2(X2)
        X3 = self.fc3(X3)
        cnt=0
        X1 = X1.unsqueeze(dim=2).unsqueeze(dim=3) * X
        X2 = X2.unsqueeze(dim=2).unsqueeze(dim=3) * X
        X3 = X3.unsqueeze(dim=2).unsqueeze(dim=3) * X
        X1 = self.weightByGrad(X1,1, Xo)
        X2 = self.weightByGrad(X2,2, Xo)
        X3 = self.weightByGrad(X3,3, Xo)

        # use stn to crop, size become (N,3,96,96)
        X1 = self.stn(X1, 0)
        X2 = self.stn(X2, 1)
        X3 = self.stn(X3, 2)

        return X


def main():
    # tmpNet=torch.nn.DataParallel(CGNN()).cuda()
    # tmpNet.load_state_dict(torch.load("preTrainedGCNetModel.pth"))
    # tmpNet.eval()
    # state_dict=tmpNet.state_dict()
    # print(state_dict)

    train_transforms = torchvision.transforms.Compose([
        torchvision.transforms.ToPILImage(),
        torchvision.transforms.Resize(size=448),  # Let smaller edge match
        torchvision.transforms.RandomHorizontalFlip(),
        torchvision.transforms.RandomCrop(size=448),
        torchvision.transforms.ToTensor(),
        torchvision.transforms.Normalize(mean=(0.485, 0.456, 0.406),
                                         std=(0.229, 0.224, 0.225))
    ])

    #####test code
    net = torch.nn.DataParallel(GTBNN()).cuda()
    print(net)
    trainset = CUB200_loader(os.getcwd() + '/data/CUB_200_2011',transform=train_transforms)
    testset = CUB200_loader(os.getcwd() + '/data/CUB_200_2011', split='test')
    train_loader = data.DataLoader(trainset, batch_size=1,
                                   shuffle=True, collate_fn=trainset.CUB_collate, num_workers=4)  # shuffle?
    test_loader = data.DataLoader(testset, batch_size=1,
                                  shuffle=False, collate_fn=testset.CUB_collate, num_workers=4)
    criterion = torch.nn.CrossEntropyLoss()
    # solver = torch.optim.SGD(
    #     net.parameters(), lr=0.1, weight_decay=1e-5)
    solver = torch.optim.Adam(net.parameters(),lr=0.01,weight_decay=1e-4)
    lrscheduler=torch.optim.lr_scheduler.CosineAnnealingLR(solver,T_max=32)

    def _accuracy(net, data_loader):
        """Compute the train/test accuracy.
        Args:
            data_loader: Train/Test DataLoader.
        Returns:
            Train/Test accuracy in percentage.
        """
        net.train(False)

        num_correct = 0
        num_total = 0
        for X, y in data_loader:
            # Data.
            X = torch.autograd.Variable(X.cuda())
            y = torch.autograd.Variable(y.cuda())
            X.requires_grad = True
            # Prediction.
            score = net(X)
            _, prediction = torch.max(score.data, 1)
            num_total += y.size(0)
            num_correct += torch.sum(prediction == y.data).item()
        net.train(True)  # Set the model to training phase
        return 100 * num_correct / num_total

    best_acc = 0.0
    best_epoch = None
    for t in range(100):
        epoch_loss = []
        num_correct = 0
        num_total = 0
        cnt = 0
        print('Epoch ' + str(t), flush=True)
        for X, y in train_loader:
            X = torch.autograd.Variable(X.cuda())
            y = torch.autograd.Variable(y.cuda())
            print(y,flush=True)
            solver.zero_grad()

            result = X.data[0].permute(1, 2, 0).cpu().numpy()
            result = normalize(result)
            print(result.size)
            plt.imshow(result)
            plt.show()

            a=input('Choose or not?')
            if(eval(a)==1):
                # Forward pass.
                X.requires_grad = True
                score = net(X)
                break
            else:
                continue

            break # for img

            loss = criterion(score, y)
            # epoch_loss.append(loss.data[0])
            epoch_loss.append(loss.data.item())
            # Prediction.
            _, prediction = torch.max(score.data, 1)
            num_total += y.size(0)
            num_correct += torch.sum(prediction == y.data)

            loss.backward()
            solver.step()
            lrscheduler.step()

            if (num_total >= cnt * 500):
                cnt += 1
                # print("Train Acc: " + str((100 * num_correct / num_total).item()) + "%" + "\n" + str(
                #     num_correct) + " " + str(num_total) + "\n" + str(prediction) + " " + str(y.data) + "\n" + str(
                #     loss.data), flush=True)
                logger.info("Train Acc: " + str((100 * num_correct / num_total).item()) + "%" + "\n" + str(
                    num_correct) + " " + str(num_total) + "\n" + str(prediction) + " " + str(y.data) + "\n" + str(
                    loss.data))
                logger.handlers[1].flush()
                # break




if __name__ == '__main__':
    # main()
    trainset = CUB200_loader(os.getcwd() + '/data/CUB_200_2011')
    train_loader = data.DataLoader(trainset, batch_size=1,
                                   shuffle=False, collate_fn=trainset.CUB_collate, num_workers=1)  # shuffle?
    theta =torch.Tensor([[1,0,-0.5],
                         [0,1,0]])
    theta = theta.view(-1,2,3)
    for X,y in train_loader:
        visualize(X[0])
        grid = F.affine_grid(theta, X.size())
        X = F.grid_sample(X, grid)
        visualize(X[0])