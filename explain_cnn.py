import argparse
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.optim.lr_scheduler import StepLR
from inverter import Inverter, ObjectiveTerm
import invert_utils
import gurobipy as gp
from gurobipy import GRB
import numpy as np
from torchsummary import summary


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.layers = nn.ModuleDict()

        self.layers["conv1"] = nn.Conv2d(1, 32, 3, 1)
        self.layers["conv1_ReLU"] = nn.ReLU()
        self.layers["conv2"] = nn.Conv2d(32, 64, 3, 1)
        self.layers["conv2_ReLU"] = nn.ReLU()
        self.layers["max_pool2d"] = nn.MaxPool2d(2)
        self.layers["dropout1"] = nn.Dropout(0.25)
        self.layers["flatten"] = nn.Flatten()
        self.layers["dropout2"] = nn.Dropout(0.5)
        self.layers["fc1"] = nn.Linear(9216, 128)
        self.layers["fc1_ReLU"] = nn.ReLU()
        self.layers["fc2"] = nn.Linear(128, 10)

    def forward(self, X):
        X = torch.Tensor(X)
        for layer in self.layers.values():
            X = layer(X)
        output = F.log_softmax(X, dim=1)
        return output

    def get_all_layer_outputs(self, X):
        X = torch.Tensor(X)
        all_outputs = [("Input", X)]
        for name, layer in self.layers.items():
            all_outputs.append((name, layer(all_outputs[-1][1])))
        return all_outputs


def train(args, nn, device, train_loader, optimizer, epoch):
    nn.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = nn(data)
        loss = F.nll_loss(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0:
            print(
                "Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}".format(
                    epoch,
                    batch_idx * len(data),
                    len(train_loader.dataset),
                    100.0 * batch_idx / len(train_loader),
                    loss.item(),
                )
            )
            if args.dry_run:
                break


def test(nn, device, test_loader):
    nn.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = nn(data)
            test_loss += F.nll_loss(
                output, target, reduction="sum"
            ).item()  # sum up batch loss
            pred = output.argmax(
                dim=1, keepdim=True
            )  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)

    print(
        "\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            test_loss,
            correct,
            len(test_loader.dataset),
            100.0 * correct / len(test_loader.dataset),
        )
    )


def train_network():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="input batch size for testing (default: 1000)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=14,
        metavar="N",
        help="number of epochs to train (default: 14)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1.0,
        metavar="LR",
        help="learning rate (default: 1.0)",
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        metavar="M",
        help="Learning rate step gamma (default: 0.7)",
    )
    parser.add_argument(
        "--no-cuda", action="store_true", default=False, help="disables CUDA training"
    )
    parser.add_argument(
        "--no-mps",
        action="store_true",
        default=False,
        help="disables macOS GPU training",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="quickly check a single pass",
    )
    parser.add_argument(
        "--seed", type=int, default=1, metavar="S", help="random seed (default: 1)"
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--save-nn",
        action="store_true",
        default=False,
        help="For Saving the current nn",
    )
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    use_mps = not args.no_mps and torch.backends.mps.is_available()

    torch.manual_seed(args.seed)

    if use_cuda:
        device = torch.device("cuda")
    elif use_mps:
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    train_kwargs = {"batch_size": args.batch_size}
    test_kwargs = {"batch_size": args.test_batch_size}
    if use_cuda:
        cuda_kwargs = {"num_workers": 1, "pin_memory": True, "shuffle": True}
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    transform = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    )
    dataset1 = datasets.MNIST("../data", train=True, download=True, transform=transform)
    dataset2 = datasets.MNIST("../data", train=False, transform=transform)
    train_loader = torch.utils.data.DataLoader(dataset1, **train_kwargs)
    test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    nn = Net().to(device)
    # nn.load_state_dict(torch.load("mnist_cnn.pt"))

    optimizer = optim.Adadelta(nn.parameters(), lr=args.lr)

    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)
    for epoch in range(1, args.epochs + 1):
        train(args, nn, device, train_loader, optimizer, epoch)
        test(nn, device, test_loader)
        scheduler.step()

    if args.save_nn:
        torch.save(nn.state_dict(), "mnist_cnn.pt")


def main():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        metavar="N",
        help="input batch size for training (default: 64)",
    )
    parser.add_argument(
        "--test-batch-size",
        type=int,
        default=1000,
        metavar="N",
        help="input batch size for testing (default: 1000)",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=14,
        metavar="N",
        help="number of epochs to train (default: 14)",
    )
    parser.add_argument(
        "--lr",
        type=float,
        default=1.0,
        metavar="LR",
        help="learning rate (default: 1.0)",
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.7,
        metavar="M",
        help="Learning rate step gamma (default: 0.7)",
    )
    parser.add_argument(
        "--no-cuda", action="store_true", default=False, help="disables CUDA training"
    )
    parser.add_argument(
        "--no-mps",
        action="store_true",
        default=False,
        help="disables macOS GPU training",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="quickly check a single pass",
    )
    parser.add_argument(
        "--seed", type=int, default=1, metavar="S", help="random seed (default: 1)"
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument(
        "--save-nn",
        action="store_true",
        default=False,
        help="For Saving the current nn",
    )
    parser.add_argument(
        "--load",
        action="store_true",
        default=False,
        help="For Saving the current nn",
    )
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()
    use_mps = not args.no_mps and torch.backends.mps.is_available()

    torch.manual_seed(args.seed)

    if use_cuda:
        device = torch.device("cuda")
    elif use_mps:
        device = torch.device("mps")
    else:
        device = torch.device("cpu")

    train_kwargs = {"batch_size": args.batch_size}
    test_kwargs = {"batch_size": args.test_batch_size}
    if use_cuda:
        cuda_kwargs = {"num_workers": 1, "pin_memory": True, "shuffle": True}
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    transform = transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]
    )
    dataset1 = datasets.MNIST("../data", train=True, download=True, transform=transform)
    # dataset2 = datasets.MNIST("../data", train=False, transform=transform)
    # train_loader = torch.utils.data.DataLoader(dataset1, **train_kwargs)
    # test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    nn = Net().to(device)
    nn.load_state_dict(torch.load("mnist_cnn.pt"))
    nn.eval()

    summary(nn, input_size=dataset1[0][0].shape)

    env = gp.Env(logfilename="")

    inverter = Inverter(args, nn, dataset1, env)

    if args.load:
        inverter.load_model()
        # TODO: Ensure correct row
        X = inverter.get_mvar("X", shape=(1, 1, 28, 28))
        inverter.set_input_vars({"X": X})
        inverter.load_inverter()
    else:
        X = inverter.m.addMVar(
            (1, 1, 28, 28), lb=-255, ub=255, vtype=GRB.CONTINUOUS, name="X"
        )

        # inverter.set_input_vars([("X", X)])
        inverter.set_input_vars({"X": X})

        # invert_utils.add_torch_conv2d_constraint(m, layer=nn.conv1, X=X)
        previous_layer_output = X
        for name, layer in nn.layers.items():
            if name == "max_pool2d":
                break
            # if name == "fc1":
            #     breakpoint()
            print("Working on layer", name)
            if isinstance(layer, torch.nn.Dropout):
                continue
            previous_layer_output = invert_utils.invert_torch_layer(
                inverter.model,
                layer,
                name=name,
                X=previous_layer_output,
            )
            inverter.output_vars[name] = previous_layer_output

        inverter.add_objective_term(
            ObjectiveTerm("obj", inverter.output_vars["conv2_ReLU"][0][0][0][0])
        )

        inverter.save_model()

        inverter.save_inverter()

    inverter.warm_start({"X": dataset1[0][0][np.newaxis, :]})

    inverter.solve()

    if inverter.m.Status in [3, 4]:  # If the model is infeasible, see why
        inverter.computeIIS()


if __name__ == "__main__":
    # train_network()
    main()