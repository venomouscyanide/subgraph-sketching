"""
testing / inference functions
"""
import time
from math import inf

import torch
from torch.utils.data import DataLoader
from tqdm import tqdm
import wandb

from evaluation import evaluate_auc, evaluate_hits, evaluate_mrr


def get_test_func(args, split):
    if args.model == 'ELPH':
        return get_gnn_preds
    if split == 'train':
        test_func = get_bulk_preds if args.bulk_train else get_preds
    elif split in {'val', 'valid'}:
        test_func = get_bulk_preds if args.bulk_val else get_preds
    elif split == 'test':
        test_func = get_bulk_preds if args.bulk_test else get_preds
    else:
        raise NotImplementedError(f'no {split} split implemented')
    return test_func


@torch.no_grad()
def test(model, evaluator, train_loader, val_loader, test_loader, args, device, emb=None, eval_metric='hits'):
    print('starting testing')
    t0 = time.time()
    model.eval()
    print("get train predictions")
    train_pred_func = get_test_func(args, 'train')
    if args.dataset_name != 'ogbl-citation2':  # can't filter if metric is mrr
        train_eval_samples = len(val_loader.dataset)  # No need for more than this to diagnose overfitting
    else:
        train_eval_samples = inf
    pos_train_pred, neg_train_pred, train_pred, train_true = train_pred_func(model, train_loader, device, args,
                                                                             train_eval_samples, split='train')
    print("get val predictions")
    val_pred_func = get_test_func(args, 'val')
    pos_val_pred, neg_val_pred, val_pred, val_true = val_pred_func(model, val_loader, device, args, split='val')
    print("get test predictions")
    test_pred_func = get_test_func(args, 'test')
    pos_test_pred, neg_test_pred, test_pred, test_true = test_pred_func(model, test_loader, device, args,
                                                                        args.test_samples, split='test')

    if eval_metric == 'hits':
        results = evaluate_hits(evaluator, pos_train_pred, neg_train_pred, pos_val_pred, neg_val_pred, pos_test_pred,
                                neg_test_pred, Ks=[args.K])
    elif eval_metric == 'mrr':

        results = evaluate_mrr(evaluator, pos_train_pred, neg_train_pred, pos_val_pred, neg_val_pred, pos_test_pred,
                               neg_test_pred)
    elif eval_metric == 'auc':
        results = evaluate_auc(val_pred, val_true, test_pred, test_true)

    print(f'testing ran in {time.time() - t0}')

    return results


@torch.no_grad()
def get_preds(model, loader, device, args, emb=None, sample_size=inf, split=None):
    # todo get rid of sample_size param, which isn't used
    sample_arg = get_sample_arg(split, args)
    if sample_arg <= 1:
        samples = len(loader.dataset) * sample_arg
    else:
        samples = sample_arg
    y_pred, y_true = [], []
    pbar = tqdm(loader, ncols=70)
    if args.wandb:
        wandb.log({f"inference_{split}_total_batches": len(loader)})
    batch_processing_times = []
    t0 = time.time()
    for batch_count, data in enumerate(pbar):
        start_time = time.time()
        if args.model == 'hashing':
            data_dev = [elem.squeeze().to(device) for elem in data]
            logits = model(*data_dev[:-1])
            y_true.append(data[-1].view(-1).cpu().to(torch.float))
        else:
            data = data.to(device)
            x = data.x if args.use_feature else None
            edge_weight = data.edge_weight if args.use_edge_weight else None
            node_id = data.node_id if emb else None
            logits = model(data.z, data.edge_index, data.batch, x, edge_weight, node_id, data.src_degree,
                           data.dst_degree)
            y_true.append(data.y.view(-1).cpu().to(torch.float))
        y_pred.append(logits.view(-1).cpu())
        batch_processing_times.append(time.time() - start_time)
        if (batch_count + 1) * args.batch_size > samples:
            del data
            torch.cuda.empty_cache()
            break
        del data
        torch.cuda.empty_cache()
    if args.wandb:
        wandb.log({f"inference_{split}_batch_time": np.mean(batch_processing_times)})
        wandb.log({f"inference_{split}_epoch_time": time.time() - t0})

    pred, true = torch.cat(y_pred), torch.cat(y_true)
    pos_pred = pred[true == 1]
    neg_pred = pred[true == 0]
    samples_used = len(loader.dataset) if sample_size > len(loader.dataset) else sample_size
    print(f'{len(pos_pred)} positives and {len(neg_pred)} negatives for sample of {samples_used} edges')
    return pos_pred, neg_pred, pred, true


@torch.no_grad()
def get_bulk_preds(model, loader, device, args, sample_size=inf, split=None):
    t0 = time.time()
    preds = []
    data = loader.dataset
    # hydrate edges
    links = data.links
    labels = torch.tensor(data.labels)
    loader = DataLoader(range(len(links)), args.eval_batch_size,
                        shuffle=False)  # eval batch size should be the largest that fits on GPU
    if model.node_embedding is not None:
        if args.propagate_embeddings:
            emb = model.propagate_embeddings_func(data.edge_index.to(device))
        else:
            emb = model.node_embedding.weight
    else:
        emb = None
    for batch_count, indices in enumerate(tqdm(loader)):
        curr_links = links[indices].to(device)
        batch_emb = None if emb is None else emb[curr_links].to(device)
        if args.use_struct_feature:
            structure_features = data.structure_features[indices].to(device)
        else:
            structure_features = torch.zeros(data.structure_features[indices].shape).to(device)
        node_features = data.x[curr_links].to(device)
        degrees = data.degrees[curr_links].to(device)
        if args.use_RA:
            RA = data.RA[indices].to(device)
        else:
            RA = None
        logits = model(structure_features, node_features, degrees[:, 0], degrees[:, 1], RA, batch_emb)
        preds.append(logits.view(-1).cpu())
        if (batch_count + 1) * args.eval_batch_size > sample_size:
            break

    if args.wandb:
        wandb.log({f"inference_{split}_epoch_time": time.time() - t0})
    pred = torch.cat(preds)
    labels = labels[:len(pred)]
    pos_pred = pred[labels == 1]
    neg_pred = pred[labels == 0]
    return pos_pred, neg_pred, pred, labels


def get_sample_arg(split, args):
    if split == 'train':
        if args.dynamic_train:
            return args.train_samples
        else:
            return inf
    elif split in {'val', 'valid'}:
        if args.dynamic_val:
            return args.val_samples
        else:
            return inf
    elif split == 'test':
        if args.dynamic_test:
            return args.test_samples
        else:
            return inf
    else:
        raise NotImplementedError(f'split: {split} is not a valid split')


@torch.no_grad()
def get_gnn_preds(model, loader, device, args, sample_size=inf, split=None):
    t0 = time.time()
    preds = []
    data = loader.dataset
    # hydrate edges
    links = data.links
    labels = torch.tensor(data.labels)
    loader = DataLoader(range(len(links)), args.eval_batch_size,
                        shuffle=False)  # eval batch size should be the largest that fits on GPU
    # get node features
    if model.node_embedding is not None:
        if args.propagate_embeddings:
            emb = model.propagate_embeddings_func(data.edge_index.to(device))
        else:
            emb = model.node_embedding.weight
    else:
        emb = None
    node_features, hashes, cards, _ = model(data.x.to(device), data.edge_index.to(device))
    for batch_count, indices in enumerate(tqdm(loader)):
        curr_links = links[indices].to(device)
        batch_emb = None if emb is None else emb[curr_links].to(device)
        if args.use_struct_feature:
            structure_features = model.elph_hashes.get_subgraph_features(curr_links, hashes, cards).to(device)
        else:
            structure_features = torch.zeros(data.structure_features[indices].shape).to(device)
        batch_node_features = None if node_features is None else node_features[curr_links]
        logits = model.predictor(structure_features, batch_node_features, batch_emb)
        preds.append(logits.view(-1).cpu())

    if args.wandb:
        wandb.log({f"inference_{split}_epoch_time": time.time() - t0})
    pred = torch.cat(preds)
    labels = labels[:len(pred)]
    pos_pred = pred[labels == 1]
    neg_pred = pred[labels == 0]
    return pos_pred, neg_pred, pred, labels