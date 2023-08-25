import torch
import torch
from sklearn.model_selection import train_test_split
from torch_geometric.utils import dense_to_sparse

from torch.nn import Linear, ModuleDict, ReLU
from torch_geometric.nn import SAGEConv
from torch_geometric.data import Data
from torch_geometric.nn.aggr import SumAggregation, MeanAggregation, MaxAggregation, Aggregation

import torch
from torch_geometric.datasets import TUDataset
from torch_geometric.loader import DataLoader
import torch
from tqdm import tqdm
import os
import pickle

class GNN(torch.nn.Module):
    aggr_classes = {
        "mean": MeanAggregation,
        "sum": SumAggregation,
        "max": MaxAggregation,
    }
    def __init__(self, in_channels, out_channels, conv_features, lin_features, global_aggr="mean", conv_aggr="mean"):
        super(GNN, self).__init__()

        self.layers = ModuleDict()

        # Add convolutional layers
        self.layers["Conv_0"] = SAGEConv(in_channels, conv_features[0], aggr=conv_aggr)
        self.layers[f"Conv_0_Relu"] = ReLU()
        for i, shape in enumerate(zip(conv_features, conv_features[1:])):
            self.layers[f"Conv_{i+1}"] = SAGEConv(*shape, aggr=conv_aggr)
            self.layers[f"Conv_{i+1}_Relu"] = ReLU()

        # Add Global Pooling Layer
        self.layers["Aggregation"] = self.aggr_classes[global_aggr]()

        # Add FC Layers
        if len(lin_features) > 0:
            self.layers["Lin_0"] = Linear(conv_features[-1], lin_features[0])
            self.layers[f"Lin_0_Relu"] = ReLU()
        if len(lin_features) > 1:
            for i, shape in enumerate(zip(lin_features, lin_features[1:])):
                self.layers[f"Lin_{i+1}"] = Linear(*shape)
                self.layers[f"Lin{i+1}Relu"] = ReLU()
        self.layers[f"Lin_Output"] = Linear(lin_features[-1] if len(lin_features)>0 else conv_features[-1], out_channels)
        
    def fix_data(self, data):
        # If the data does not have any batches, assign all the nodes to the same batch
        if data.batch is None: data.batch = torch.zeros(data.x.shape[0], dtype=torch.int64)
        # If there are no edge weights, assign weight 1 to all edges
        if data.edge_weight is None: data.edge_weight = torch.ones(data.edge_index.shape[1])
        return data
    
    def forwardXA(self, X, A):
        # Same as forward, but takes node features and adjacency matrix instead of a Data object
        X = torch.Tensor(X).double()
        A = torch.Tensor(A)
        edge_index, edge_weight = dense_to_sparse(A)
        data = Data(x=X, edge_index=edge_index, edge_weight=edge_weight)
        return self.forward(data)

    def forward(self, data):
        data = self.fix_data(data)
        x = data.x
        for layer in self.layers.values():
            if isinstance(layer, SAGEConv):
                x = layer(x, data.edge_index)
            elif isinstance(layer, Aggregation):
                x = layer(x, data.batch)
            else:
                x = layer(x)
        return x
    
    def get_all_layer_outputs(self, data):
        data = self.fix_data(data)
        outputs = [("Input", data.x)]
        for name, layer in self.layers.items():
            if isinstance(layer, SAGEConv):
                outputs.append((name, layer(outputs[-1][1], data.edge_index)))
            else:
                outputs.append((name, layer(outputs[-1][1])))
        return outputs
    
def train(model, train_loader, optimizer, criterion):
    model.train()
    total_loss = 0
    for data in train_loader:  # Iterate in batches over the training dataset
        out = model(data) # Perform a single forward pass
        loss = criterion(out, data.y)  # Compute the loss.
        loss.backward()  # Derive gradients.
        optimizer.step()  # Update parameters based on gradients.
        optimizer.zero_grad()  # Clear gradients.
        total_loss += float(loss)
    return total_loss/len(train_loader) # Return average batch loss

@torch.no_grad()
def test(model, loader):
    model.eval()

    correct = 0
    for data in loader:  # Iterate in batches over the training/test dataset.
        out = model(data) 
        pred = out.argmax(dim=1)  # Use the class with highest probability.
        correct += int((pred == data.y).sum())  # Check against ground-truth labels.
    return correct / len(loader.dataset)  # Derive ratio of correct predictions.

if __name__ == "__main__":
    torch.manual_seed(12345)

    if not os.path.isdir("models"): os.mkdir("models")

    epochs = 100
    num_inits = 5
    num_explanations = 3
    conv_type = "sage"
    global_aggr = "sum"
    conv_aggr = "mean"

    load_model = False
    # model_path = "models/MUTAG_model.pth"
    model_path = "models/OurMotifs_model_mean.pth"

    log_run = False

    # from torch_geometric.datasets import ExplainerDataset, BA2MotifDataset, BAMultiShapesDataset
    # from torch_geometric.datasets.graph_generator import BAGraph
    # from torch_geometric.datasets.motif_generator import HouseMotif
    # from torch_geometric.datasets.motif_generator import CycleMotif

    # dataset = TUDataset(root="data/TUDataset", name="MUTAG")
    # print(dataset[0].x)
    with open("data/OurMotifs/dataset.pkl", "rb") as f:
        dataset = pickle.load(f)

    print()
    print(f'Dataset: {dataset}:')
    print('====================')
    print(f'Number of graphs: {len(dataset)}')
    # print(f'Number of features: {dataset.num_features}')
    # print(f'Number of classes: {dataset.num_classes}')

    # train_dataset, test_dataset = train_test_split(dataset, train_size=0.8, stratify=dataset.y, random_state=7)
    ys = [d.y for d in dataset]
    num_classes = len(set(ys))
    num_node_features = dataset[0].x.shape[1]
    train_dataset, test_dataset = train_test_split(dataset, train_size=0.8, stratify=ys, random_state=7)

    print(f'Number of training graphs: {len(train_dataset)}')
    print(f'Number of test graphs: {len(test_dataset)}')
    print()

    train_loader = DataLoader(train_dataset, batch_size=16, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=16, shuffle=False)

    if not load_model:
        model = GNN(in_channels=num_node_features, out_channels=num_classes, conv_features=[4,4], lin_features=[4], global_aggr=global_aggr, conv_aggr=conv_aggr)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
        criterion = torch.nn.CrossEntropyLoss()
        print(model)
        pbar = tqdm(range(1,epochs+1))
        for epoch in pbar:
            avg_loss = train(model, train_loader, optimizer, criterion)
            train_acc = test(model, train_loader)
            test_acc = test(model, test_loader)
            pbar.set_postfix_str(f'Epoch: {epoch:03d}, Train Acc: {train_acc:.4f}, Test Acc: {test_acc:.4f}, Avg Loss: {avg_loss:.4f}')

        torch.save(model, model_path)
    else:
        model = torch.load(model_path)
        test_acc = test(model, test_loader)
        print(f"Test Accuracy: {test_acc:.4f}")