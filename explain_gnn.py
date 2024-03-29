import torch
import gurobipy as gp
from gurobipy import GRB
from torch_geometric.utils import to_dense_adj, dense_to_sparse
from torch_geometric.data import Data
import networkx as nx
import os
import pickle
import matplotlib.pyplot as plt
from torch_geometric.utils import to_networkx
from arg_parser import parse_args
from datasets import get_dataset
from inverter import Inverter, ObjectiveTerm
import invert_utils
import numpy as np
import random
from gnn import GNN  # noqa: F401
import time

args = parse_args()

dataset_name = args.dataset_name
model_path = args.model_path
max_class = args.max_class
output_file = args.output_file
sim_weights = dict(zip(args.regularizers, args.regularizer_weights))
sim_methods = args.regularizers
num_nodes = args.num_nodes
device = (
    args.device
    if args.device is not None
    else torch.device("cuda" if torch.cuda.is_available() else "cpu")
)

if not model_path:
    model_path = f"models/{dataset_name}_model.pth"

# torch.manual_seed(12345)
# TODO: Seed for Gurobi

if not os.path.isdir("solutions"):
    os.mkdir("solutions")

dataset = get_dataset(dataset_name)
num_node_features = dataset.num_node_features


def canonicalize_graph(graph):
    # This function will reorder the nodes of a given graph (PyTorch Geometric "Data" Object) to a canonical (maybe) version

    G = to_networkx(init_graph)

    # TODO: Generalize to non one-hot vector node features
    # Lexicographic ordering of node features (one-hot)
    feature_ordering = np.argsort(np.argmax(graph.x.detach().numpy(), axis=1))
    node_degree_ordering = to_dense_adj(graph.edge_index).squeeze().sum(axis=1)
    lexicographic_ordering = np.lexsort((feature_ordering, node_degree_ordering))

    # Sort by node degree, then by lexicographic ordering
    ## DFS to get a node ordering, prioritizing first by node degree, then by lexicographic ordering of node features
    def lexicographic_dfs_ordering(G, node, visited):
        visited[node] = True
        ## Get the neighbors of the current node
        neighbors = list(G.neighbors(node))
        ## Sort them lexicographically by featuer and degree
        tree_order = [node]
        for node in feature_ordering:
            if node in neighbors:
                if not visited[node]:
                    subtree_order = lexicographic_dfs_ordering(G, node, visited)
                    tree_order.extend(subtree_order)
        return tree_order

    visited = [False] * len(graph.x)
    sorted_nodes = lexicographic_dfs_ordering(G, lexicographic_ordering[0], visited)

    # # Get node ordering
    # sorted_nodes = list(nx.dfs_preorder_nodes(G, source=min_node))
    # sorted_nodes.reverse()

    # Create a mapping of old labels to new labels
    label_mapping = {node: i for i, node in enumerate(sorted_nodes)}

    # Relabel the graph
    G = nx.relabel_nodes(G, label_mapping)

    graph.x = graph.x[sorted_nodes, :]
    graph.edge_index = torch.Tensor(list(G.edges)).to(torch.int64).T


# Load the model
nn = torch.load(model_path, fix_imports=True, map_location=device)
nn.device = device
nn.eval()
nn.to(torch.float64)

# Track hyperparameters
if args.log:
    import wandb

    wandb.login()
    config = {
        "architecture": str(nn),
        "model_path": model_path,
    }
    config.update(vars(args))
    wandb.init(
        project="GNN-Inverter",
        config=config,
    )
    wandb.save(args.param_file, policy="now")
    wandb.save(output_file, policy="end")
    wandb.run.log_code(".")

print("Args:", args)
print("Device:", device)
print("Number of Classes", dataset.num_classes)
print("Number of Node Features", num_node_features)

# # Max nn Output Logit for each class in the dataset
# max_logits = [0] * dataset.num_classes
# for i, graph in enumerate(dataset):
#     logits = nn(graph).detach().numpy().squeeze()
#     for j, logit in enumerate(logits):
#         if logit > max_logits[j]:
#             max_logits[j] = logit
# print("Max Logits:", max_logits)

if args.init_with_data:
    # Initialize with a graph from the dataset
    if args.init_index is not None:
        print(f"Initializing from dataset with graph at index {args.init_index}")
        init_graph = dataset[args.init_index]
    elif num_nodes is not None:
        print(f"Initializing from dataset graph with {num_nodes} nodes")
        init_graph = random.choice(
            [d for d in dataset if int(d.y) == max_class and d.num_nodes == num_nodes]
        )
    A = to_dense_adj(init_graph.edge_index).detach().numpy().squeeze()
    print(
        "Connected before reordering:",
        all(
            [
                sum(A[i][j] + A[j][i] for j in range(i + 1, A.shape[0])) >= 1
                for i in range(A.shape[0] - 1)
            ]
        ),
    )

    A = to_dense_adj(init_graph.edge_index).detach().numpy().squeeze()
    assert all(
        [
            sum(A[i][j] + A[j][i] for j in range(i + 1, A.shape[0])) >= 1
            for i in range(A.shape[0] - 1)
        ]
    ), "Initialization graph was not connected"

    num_nodes = init_graph.num_nodes

else:
    # Initialize with a dummy graph
    # By default, will generate a line graph with uniform random node features
    print("Initializing with dummy graph")
    # init_graph_adj = np.clip(init_graph_adj + np.eye(num_nodes, k=1), a_min=0, a_max=1)
    # init_graph_adj = torch.diag_embed(
    #     torch.diag(torch.ones((num_nodes, num_nodes)), diagonal=-1), offset=-1
    # ) + torch.diag_embed(
    #     torch.diag(torch.ones((num_nodes, num_nodes)), diagonal=1), offset=1
    # )
    ## Randomly initialized adjacency matrix of a connected graph
    init_graph_adj = torch.randint(0, 2, (num_nodes, num_nodes))
    init_graph_adj = torch.triu(init_graph_adj, diagonal=1)
    init_graph_adj = init_graph_adj + init_graph_adj.T
    init_graph_adj = torch.clip(init_graph_adj, 0, 1)
    init_graph_adj = init_graph_adj.numpy()
    init_graph_adj = np.clip(init_graph_adj + np.eye(num_nodes, k=1), a_min=0, a_max=1)
    init_graph_adj = np.clip(init_graph_adj + np.eye(num_nodes, k=-1), a_min=0, a_max=1)
    init_graph_adj = torch.Tensor(init_graph_adj)

    if dataset_name in ["Is_Acyclic", "Shapes", "Shapes_Clean"]:
        init_graph_x = torch.unsqueeze(torch.sum(init_graph_adj, dim=-1), dim=-1)
    elif dataset_name in ["MUTAG", "OurMotifs"]:
        # init_graph_x = torch.eye(num_node_features)[torch.randint(num_node_features, (num_nodes,)),:]
        init_graph_x = torch.eye(num_node_features)[torch.randint(1, (num_nodes,)), :]
    elif dataset_name in ["Shapes_Ones", "Is_Acyclic_Ones"]:
        init_graph_x = torch.ones((num_nodes, num_node_features))

    # init_graph_adj = torch.randint(0, 2, (num_nodes, num_nodes))
    # init_graph_adj = torch.ones((num_nodes, num_nodes))
    init_graph = Data(x=init_graph_x, edge_index=dense_to_sparse(init_graph_adj)[0])

print(nn)
num_model_params = sum(param.numel() for param in nn.parameters())
print("Model Parameters:", num_model_params)
if args.log:
    wandb.run.summary["# Model Parameter"] = num_model_params

env = gp.Env(logfilename="")


def convert_inputs(X, A):
    X = torch.Tensor(X)
    A = torch.Tensor(A)
    return {"data": Data(x=X, edge_index=dense_to_sparse(A)[0])}


start_time = time.time()

inverter = Inverter(args, nn, dataset, env, convert_inputs)
m = inverter.model

# Add and constrain decision variables for adjacency matrix
A = m.addMVar((num_nodes, num_nodes), vtype=GRB.BINARY, name="A")
invert_utils.force_connected(m, A)
invert_utils.force_undirected(m, A)
invert_utils.remove_self_loops(m, A)
# m.addConstr(gp.quicksum(A) >= 1, name="non_isolatied") # Nodes need an edge. Need this for SAGEConv inverse to work. UNCOMMENT IF NO OTHER CONSTRAINTS DO THIS

# Add and constrain decision variables for node feature matrix
if dataset_name in ["MUTAG", "OurMotifs"]:
    X = m.addMVar((num_nodes, num_node_features), vtype=GRB.BINARY, name="X")
    m.addConstr(gp.quicksum(X.T) == 1, name="categorical_features")
elif dataset_name in ["Is_Acyclic", "Shapes", "Shapes_Clean"]:
    X = m.addMVar(
        (num_nodes, num_node_features),
        lb=0,
        ub=init_graph.num_nodes,
        name="X",
        vtype=GRB.INTEGER,
    )
    m.addConstr(X == gp.quicksum(A)[:, np.newaxis], name="features_are_node_degrees")
elif dataset_name in ["Shapes_Ones", "Is_Acyclic_Ones"]:
    X = m.addMVar((num_nodes, num_node_features), vtype=GRB.BINARY, name="X")
    m.addConstr(X == 1, name="features_are_ones")
    X.setAttr("lb", 1)
    X.setAttr("ub", 1)
else:
    raise ValueError(f"Unknown Decision Variables for {dataset_name}")

inverter.set_input_vars({"X": X, "A": A})
inverter.set_tracked_vars({"X": X, "A": A})

# if args.log:
#     wandb.run.tags += ("MaxDeg",)
# if dataset_name == "MUTAG":
#     print("MUTAG: Adding Node Degree Constraint")
#     m.addConstr(
#         gp.quicksum(A)
#         <= 4 * X[:, 0]
#         + 3 * X[:, 1]
#         + 2 * X[:, 2]
#         + 1 * X[:, 3]
#         + 1 * X[:, 4]
#         + 1 * X[:, 5]
#         + 1 * X[:, 6],
#         name="max_node_degree",
#     )  #! DO YOU WANT THIS?

# invert_utils.order_onehot_features(inverter.m, A, X) # TODO: See if this works better for MUTAG

canonicalize_graph(init_graph)
# # Test the canonicalization with the constraints
# A.Start = to_dense_adj(init_graph.edge_index).squeeze().detach().numpy()
# inverter.solve()
# breakpoint()
# inverter.computeIIS()

## Build a MIQCP for the trained neural network
## For each layer, create and constrain decision variables to represent the output
debug_start = False
if debug_start:
    ## If in Debug Mode, we add layers one at a time and fix them to their starting values. If the model becomes infeasible, we can diagnose the problem by computing a minimal IIS
    previous_layer_output = X
    X.start = init_graph.x.detach().numpy()
    A.start = to_dense_adj(init_graph.edge_index).squeeze().detach().numpy()
    all_layer_outputs = dict(nn.get_all_layer_outputs(init_graph))
    fixing_constraints = [inverter.model.addConstr(X == init_graph.x.detach().numpy())]
    old_numvars = 0
    old_numconstrs = 0
    for name, layer in nn.layers.items():
        inverter.model.update()
        print("Encoding Layer:", name)
        previous_layer_output = invert_utils.invert_torch_layer(
            inverter.model,
            layer,
            name=name,
            X=previous_layer_output,
            A=A,
        )
        inverter.output_vars[name] = previous_layer_output
        assert inverter.output_vars[name].shape == all_layer_outputs[name].shape
        inverter.output_vars[name].Start = all_layer_outputs[name].detach().numpy()
        fixing_constraints.append(
            inverter.model.addConstr(
                inverter.output_vars[name] == all_layer_outputs[name].detach().numpy(),
                name=f"fix_{name}",
            )
        )
        inverter.model.update()
        numvars = inverter.model.NumVars
        numconstrs = inverter.model.NumConstrs
        print(
            "Number of variables:",
            numvars,
            "Number of constraints:",
            numconstrs,
            "Old Number of variables:",
            old_numvars,
            "Old Number of constraints:",
            old_numconstrs,
        )
        inverter.model.optimize()
        if not inverter.model.Status == GRB.OPTIMAL:
            print("============ PROBLEM WITH LAYER:", name, "=================")
            print(
                "Fixed:",
                set(
                    v.varName.split("[")[0]
                    for v in inverter.model.getVars()[:old_numvars]
                ),
            )
            print(
                "Fixed:",
                set(
                    c.ConstrName.split("[")[0]
                    for c in inverter.model.getConstrs()[:old_numconstrs]
                ),
            )
            print(
                "Using:",
                set(
                    v.varName.split("[")[0]
                    for v in inverter.model.getVars()[old_numvars:]
                ),
            )
            print(
                "Using:",
                set(
                    c.ConstrName.split("[")[0]
                    for c in inverter.model.getConstrs()[old_numconstrs:]
                ),
            )
            lbpen = [1.0] * (numvars - old_numvars)
            ubpen = [1.0] * (numvars - old_numvars)
            rhspen = [1.0] * (numconstrs - old_numconstrs)

            print(
                "feasRelax Result:",
                inverter.model.feasRelax(
                    0,
                    False,
                    inverter.model.getVars()[old_numvars:],
                    lbpen,
                    ubpen,
                    inverter.model.getConstrs()[old_numconstrs:],
                    rhspen,
                ),
            )
            inverter.model.optimize()
            if inverter.model.Status == GRB.OPTIMAL:
                print("\nSlack values:")
                slacks = inverter.model.getVars()[numvars:]
                for sv in slacks:
                    if sv.X > 1e-9:
                        print("%s = %g" % (sv.VarName, sv.X))
            else:
                inverter.computeIIS()
            import sys

            sys.exit()
        old_numvars = numvars
        old_numconstrs = numconstrs

    inverter.model.remove(fixing_constraints)
else:
    previous_layer_output = X
    for name, layer in nn.layers.items():
        inverter.model.update()
        previous_layer_output = invert_utils.invert_torch_layer(
            inverter.model,
            layer,
            name=name,
            X=previous_layer_output,
            A=A,
        )
        inverter.output_vars[name] = previous_layer_output

## Create decision variables to represent (unweighted) regularizer terms based on embedding similarity/distance
## These can also be used in constraints!!!
embedding = inverter.output_vars["Aggregation"][0]
regularizers = {}
if sim_methods:
    # Each row of phi is the average embedding of the graphs in the corresponding class of the dataset
    phi = dataset.get_average_phi(nn, "Aggregation")
if "Cosine" in sim_methods:
    var, calc = invert_utils.get_cosine_similarity(
        inverter.model, embedding, phi[max_class]
    )
    inverter.add_objective_term(
        ObjectiveTerm(
            name="Cosine Similarity",
            var=var,
            calc=calc,
            weight=sim_weights["Cosine"],
            required_vars=[embedding],
        ),
    )
if "L2" in sim_methods:
    var, calc = invert_utils.get_l2_distance(inverter.model, embedding, phi[max_class])
    inverter.add_objective_term(
        ObjectiveTerm(
            name="L2 Distance",
            var=var,
            calc=calc,
            weight=sim_weights["L2"],
            required_vars=[embedding],
        ),
    )
if "Squared L2" in sim_methods:
    var, calc = invert_utils.get_l2_distance(inverter.model, embedding, phi[max_class])
    inverter.add_objective_term(
        ObjectiveTerm(
            name="Squared L2 Distance",
            var=var,
            calc=calc,
            weight=sim_weights["Squared L2"],
            required_vars=[embedding],
        ),
    )
m.update()

# List of decision variables representing the logits that are not the max_class logit
other_outputs_vars = [
    inverter.output_vars["Output"][0, j]
    for j in range(dataset.num_classes)
    if j != max_class
]

# # Create a decision variable and constrain it to the maximum of the non max_class logits
other_outputs_max = m.addVar(
    name="other_outputs_max",
    lb=max(v.getAttr("lb") for v in other_outputs_vars),
    ub=max(v.getAttr("ub") for v in other_outputs_vars),
)
m.addGenConstrMax(other_outputs_max, other_outputs_vars, name="max_of_other_outputs")

max_output_var = inverter.output_vars["Output"][0, max_class]

## MIQCP objective function
inverter.add_objective_term(ObjectiveTerm("Target Class Output", max_output_var))
inverter.add_objective_term(
    ObjectiveTerm("Max Non-Target Class Output", other_outputs_max, weight=-1)
)

m.update()

# Save a copy of the model
model_files = inverter.save_model()
if args.log:
    for fn in model_files:
        wandb.save(fn, policy="now")


# Define the callback function for the solver to save intermediate solutions, other metrics
mip_information = []


def callback(model, where):
    global mip_information
    inverter.get_default_callback()(model, where)
    if where == GRB.Callback.MIPSOL:
        print("New Solution Found:", len(inverter.solutions))
        if args.log and inverter.solutions:
            solution = inverter.solutions[-1]
            fig, _ = dataset.draw_graph(A=solution["A"], X=solution["X"])
            # plt.savefig("test.png")
            wandb.log(solution, commit=False)
            wandb.log(
                {
                    f"Output Logit {i}": solution["Output"].squeeze()[i]
                    for i in range(solution["Output"].shape[1])
                },
                commit=False,
            )
            wandb.log({"fig": wandb.Image(fig)})
            plt.close()

        # with open(output_file, "wb") as f:
        #     pickle.dump(inverter.solutions, f)
    elif where == GRB.Callback.MIP:
        # Access MIP information when upper bound is updated
        runtime = model.cbGet(GRB.Callback.RUNTIME)
        if mip_information and runtime - mip_information[-1]["Runtime"] < 1:
            return
        obj_bound = model.cbGet(GRB.Callback.MIP_OBJBST)
        best_bound = model.cbGet(GRB.Callback.MIP_OBJBND)
        node_count = model.cbGet(GRB.Callback.MIP_NODCNT)
        explored_node_count = model.cbGet(GRB.Callback.MIP_NODCNT)
        unexplored_node_count = model.cbGet(GRB.Callback.MIP_NODLFT)
        cut_count = model.cbGet(GRB.Callback.MIP_CUTCNT)
        work_units = model.cbGet(GRB.Callback.WORK)

        # Save the information to a dictionary
        mip_info = {
            "ObjBound": obj_bound,
            "BestBound": best_bound,
            "NodeCount": node_count,
            "ExploredNodeCount": explored_node_count,
            "UnexploredNodeCount": unexplored_node_count,
            "CutCount": cut_count,
            "Runtime": runtime,
            "WorkUnits": work_units,
        }

        if args.log:
            wandb.log(mip_info)

        mip_information.append(mip_info)


## Warm start - create an initial solution for the model
bound_summary = inverter.warm_start(
    {"X": init_graph.x, "A": to_dense_adj(init_graph.edge_index).squeeze()},
    debug_mode=False,
)
print(bound_summary)
if args.log:
    wandb.run.summary.update(bound_summary)

# Get solver parameters
m.read(args.param_file)

# Run Optimization
inverter.solve(
    callback,
    TimeLimit=round(3600 * 2),
)

# Save all solutions
with open(output_file, "wb") as f:
    pickle.dump(inverter.solutions, f)

run_data = {"mip_information": mip_information, "solutions": inverter.solutions}

if args.log:
    image_dir = f"./results/{dataset_name}/"
    if not os.path.isdir(image_dir):
        os.makedirs(image_dir)
    imgname = f"{max_class}_{num_nodes}_{wandb.run.id}"
    fig, ax = dataset.draw_graph(
        A=inverter.solutions[0]["A"], X=inverter.solutions[0]["X"]
    )
    # fig.savefig(image_dir + imgname + "_init.png")
    run_data["initialization"] = fig
    run_data["initialization_output"] = inverter.solutions[0]["Output"].squeeze()
    fig, ax = dataset.draw_graph(
        A=inverter.solutions[-1]["A"], X=inverter.solutions[-1]["X"]
    )
    # fig.savefig(image_dir + imgname + "_solution.png")
    run_data["solution"] = fig
    run_data["solution_output"] = inverter.solutions[-1]["Output"].squeeze()

print("Model Status:", m.Status)

save_file = f"solutions/{dataset_name}_{max_class}_{num_nodes}.pkl"

if m.Status in [3, 4]:  # If the model is infeasible, see why
    inverter.computeIIS()

end_time = time.time()
run_data["runtime"] = end_time - start_time

if args.log:
    wandb.run.summary["Model Status"] = m.Status
    wandb.run.summary["Node Count"] = m.NodeCount
    wandb.run.summary["Open Node Count"] = m.OpenNodeCount
    wandb.run.summary["MIPGap"] = m.MIPGap

    for key in wandb.run.summary.keys():
        run_data[key] = wandb.run.summary[key]
    del run_data["fig"]
    for key in wandb.config.keys():
        run_data[key] = wandb.config[key]
    run_data_keys = list(run_data.keys())

    ## Temporary Solution TODO: Remove non-picklable objects
    for key in run_data_keys:
        try:
            pickle.dumps(run_data[key])
        except:
            del run_data[key]

    if not os.path.isdir(f"results/runs_{dataset_name}"):
        os.mkdir(f"results/runs_{dataset_name}")
    with open(f"results/runs_{dataset_name}/{wandb.run.id}.pkl", "wb") as f:
        pickle.dump(run_data, f)
