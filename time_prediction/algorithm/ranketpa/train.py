# -*- coding: utf-8 -*-
import os
import torch.nn as nn
import torch.nn.functional as F

from utils.util import *
from utils.eval import Metric
from algorithm.ranketpa.dataset import RankEptaDataset, ModelDataset

from algorithm.ranketpa.time_predictor import RankEPTA

def eta_mae_loss_calc(V_at, label_len, eta, label_route):
    N = eta.shape[1]
    B = V_at.shape[0]
    T = V_at.shape[1]
    V_at = V_at.reshape(B*T, N)
    label_route = label_route.reshape(B * T, N)
    label_len = label_len.reshape(B * T, 1)
    pred_result = torch.empty(0).to(V_at.device)
    label_result = torch.empty(0).to(V_at.device)
    for i in range(len(eta)):
        pred_result = torch.cat([pred_result, eta[i][label_route[i][:label_len[i].long().item()]]])
        label_result = torch.cat([label_result, V_at[i][:label_len[i].long().item()]])

    return F.l1_loss(pred_result.squeeze(1), label_result)

def process_batch_route(batch, model, device, params):
    def build_loss(outputs, target, pad_value):
        unrolled = outputs.view(-1, outputs.size(-1))
        return F.cross_entropy(unrolled, target.long().view(-1), ignore_index=pad_value)

    batch = to_device(batch, device)
    V, V_len, V_reach_mask, start_fea, start_idx, label, label_len, V_at = batch
    outputs, pointers = model(V, V_reach_mask)
    loss = build_loss(outputs, label, params['pad_value'])

    return outputs, loss


def mask_eta_loss(pred, label):
    loss_func = nn.MSELoss().to(pred.device)
    mask = label > 0
    label = label.masked_select(mask)
    pred = pred.masked_select(mask)
    loss = loss_func(pred, label)
    n = mask.sum().item()
    return loss, n

def process_batch_eta(batch, model, device, params):
    batch =  to_device(batch, device)

    V, V_reach_mask, label, label_len, V_at, sort_idx = batch

    eta = model(V, V_reach_mask, sort_idx)

    eta_loss = eta_mae_loss_calc(V_at, label_len, eta, label)

    return eta_loss

def get_eta_result(pred, label, label_len, label_route):
    N = label_route.shape[2]
    B = label_route.shape[0]
    T = label_route.shape[1]
    pred_result = torch.zeros(B*T, N).to(label.device)
    label_result = torch.zeros(B*T, N).to(label.device)
    label_len = label_len.reshape(B*T)
    label = label.reshape(B*T, N)

    label_len_list = []
    eta_pred_list = []
    eta_label_list = []

    label_route = label_route.reshape(B * T, N)
    for i in range(B*T):
        if label_len[i].long().item() != 0:
            pred_result[i][:label_len[i].long().item()] = pred[i][label_route[i][:label_len[i].long().item()]].squeeze(1)
            label_result[i][:label_len[i].long().item()] = label[i][:label_len[i].long().item()]

    for i in range(B*T):
        if label_len[i].long().item() != 0:
            eta_label_list.append(label_result[i].detach().cpu().numpy().tolist())
            eta_pred_list.append(pred_result[i].detach().cpu().numpy().tolist())
            label_len_list.append(label_len[i].detach().cpu().numpy().tolist())

    return  torch.LongTensor(label_len_list), torch.LongTensor(eta_pred_list), torch.LongTensor(eta_label_list)

def test_model_eta(model, test_dataloader, device, pad_value, params, save2file, mode):
    from utils.eval import Metric
    model.eval()
    evaluators = [Metric([1, 5]), Metric([1, 11]), Metric([1, 15]), Metric([1, 25])]

    with torch.no_grad():

        for batch in tqdm(test_dataloader):
            batch = to_device(batch, device)
            V, V_reach_mask, label, label_len, V_at, sort_idx = batch
            eta = model(V, V_reach_mask, sort_idx)
            label_len, eta_pred, eta_label = get_eta_result(eta, V_at, label_len, label)
            for e in evaluators:
                e.update_eta(label_len, eta_pred, eta_label)
    for e in evaluators:
        print(e.eta_to_str())
        params_save = dict_merge([e.eta_to_dict(), params])
        params_save['eval_min'],params_save['eval_max'] = e.len_range
        save2file(params_save)
    return evaluators[-1]

def train_val_test(train_loader, val_loader, test_loader, model, device, process_batch, test_model, params, save2file):
    if params['task'] == 'time_predict':
        mode = 'minimize'
        evalate_metric = 'mae'
    elif params['task'] == 'route_predict':
        mode = 'maximize'
        evalate_metric = 'krc'


    model.to(device)
    optimizer = Adam(model.parameters(), lr=params['lr'], weight_decay=params['wd'])
    early_stop = EarlyStop(mode=mode, patience=3)
    model_name = model.model_file_name() + f'{time.time()}'
    model_path = ws + f'/data/dataset/{params["dataset"]}/{params["task"]}/{model_name}'
    params['model_path'] = model_path
    dir_check(model_path)
    for epoch in range(params['num_epoch']):
        if early_stop.stop_flag: break
        postfix = {"epoch": epoch, "loss": 0.0, "current_loss": 0.0}
        with tqdm(train_loader, total=len(train_loader), postfix=postfix) as t:
            ave_loss = None
            model.train()
            for i, batch in enumerate(t):
                loss = process_batch(batch, model, device, params)
                if ave_loss is None:
                    ave_loss = loss.item()
                else:
                    ave_loss = ave_loss * i / (i + 1) + loss.item() / (i + 1)
                postfix["loss"] = ave_loss
                postfix["current_loss"] = loss.item()
                t.set_postfix(**postfix)

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
        if params['is_test']: break #just train one epoch for test the code
        if params['task'] == 'time_predict':
            val_result = test_model(model, val_loader, device, params['pad_value'], params, save2file, 'val')

            print('\nval result:', val_result.eta_to_str(), f'Best {evalate_metric}:', round(early_stop.best_metric(),3), '| Best epoch:', early_stop.best_epoch)
            is_best_change = early_stop.append(val_result.eta_to_dict()[evalate_metric])

            if is_best_change:
                print('value:',val_result.eta_to_dict()[evalate_metric], early_stop.best_metric())
                torch.save(model.state_dict(), model_path)
                print('best model saved')
                print('model path:', model_path)

            if params['is_test']:
                print('model_path:', model_path)
                torch.save(model.state_dict(), model_path)
                print('best model saved !!!')
                break

        elif params['task'] == 'route_predict':
            val_result = test_model(model, val_loader, device, params['pad_value'], params, save2file, 'val')
            print('\nval result:', val_result.to_str(), f'Best {evalate_metric}:', round(early_stop.best_metric(),3), '| Best epoch:', early_stop.best_epoch)
            is_best_change = early_stop.append(val_result.to_dict()[evalate_metric])

            if is_best_change:
                print('value:',val_result.to_dict()[evalate_metric], early_stop.best_metric())
                torch.save(model.state_dict(), model_path)
                print('best model saved')
                print('model path:', model_path)

            if params['is_test']:
                print('model_path:', model_path)
                torch.save(model.state_dict(), model_path)
                print('best model saved !!!')
                break

    try:
        print('loaded model path:', model_path)
        model.load_state_dict(torch.load(model_path))
        print('best model loaded !!!')
    except:
        print('load best model failed')

    if params['task'] == 'time_predict':
        test_result = test_model(model, test_loader, device, params['pad_value'],params, save2file, 'test')
        print('\n-------------------------------------------------------------')
        print('Best epoch: ', early_stop.best_epoch)
        print(f'{params["model"]} Evaluation in test:', test_result.eta_to_str())
        nni.report_final_result(test_result.eta_to_dict()[evalate_metric])
    elif params['task'] == 'route_predict':
        test_result = test_model(model, test_loader, device, params['pad_value'],params, save2file, 'test')
        print('\n-------------------------------------------------------------')
        print('Best epoch: ', early_stop.best_epoch)
        print(f'{params["model"]} Evaluation in test:', test_result.to_str())
        nni.report_final_result(test_result.to_dict()[evalate_metric])
    return params

def main(params):
    params['task'] = 'time_predict'  # route_predict before time_predict
    params['model_path'] = ''
    params['sort_x_size'] = 6
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if params['task'] == 'route_predict':
        #run the route predictor to obtain route prediction results first
        params['model'] = 'ranketpa'
        params['pad_value'] = params['max_task_num'] - 1

        # load the trained route predictor
        model, save2file = get_model_function(params['model'])
        rp_model = model(params)
        rp_model_path = params['model_path']
        device = torch.device(f'cuda:{params["cuda_id"]}' if torch.cuda.is_available() else 'cpu')
        if rp_model_path != '':
            rp_model.load_state_dict(torch.load(rp_model_path))
        rp_model.to(device)
        params['train_path'], params['val_path'], params['test_path'] = get_dataset_path(params)
        def save_route_predictions(model, mode, params):
            dataset = RankEptaDataset(mode=mode, params=params) # mode == 'train', 'val', 'test'
            data_loader = DataLoader(dataset, batch_size=params['batch_size'], shuffle=False, drop_last=False)
            model.eval()

            route_predicts = torch.empty(0).to(device)
            for batch in tqdm(data_loader):
                batch = to_device(batch, device)
                V, V_len, V_reach_mask, start_fea, start_idx, route_label, label_len, time_label = batch
                B, T, N = V_reach_mask.shape
                outputs, pointers = model(V, V_reach_mask)
                pointers = pointers.reshape(B, T, N)
                route_predicts = torch.cat([route_predicts, pointers], dim=0)

            route_predicts = route_predicts.detach().cpu().numpy()

            dataset = params['dataset']
            fout = ws + f'/data/dataset/{dataset}/{mode}_route_predict.npy'
            np.save(fout, route_predicts)

        # make and save route predictions
        for mode in ['train', 'val', 'test']:
            save_route_predictions(rp_model, mode, params)

    elif params['task'] == 'time_predict':

        def get_order_data(params):
            file_path = ws + f'/data/dataset/{params["dataset"]}'
            train_file_path = file_path + '/train_route_predict.npy'
            val_file_path = file_path + '/val_route_predict.npy'
            test_file_path = file_path + '/test_route_predict.npy'
            train_sort_idx = np.load(train_file_path, allow_pickle=True)
            val_sort_idx = np.load(val_file_path, allow_pickle=True)
            test_sort_idx = np.load(test_file_path, allow_pickle=True)
            return train_sort_idx, val_sort_idx, test_sort_idx
        
        def get_dataloader(data_list, sort_id_list):
            dataloader_list = []
            for i in range(len(data_list)):
                V = data_list[i]['V']
                V_reach_mask = data_list[i]['V_reach_mask']
                route_label = data_list[i]['route_label']
                label_len = data_list[i]['label_len']
                time_label =  data_list[i]['time_label']
                sort_idx = sort_id_list[i]
                sample_num = len(V)
                data_loader = DataLoader(dataset=ModelDataset(V, V_reach_mask, route_label, label_len, time_label, sort_idx, sample_num), batch_size=params['batch_size'], shuffle=False, drop_last=True)
                dataloader_list.append(data_loader)
            return dataloader_list[0], dataloader_list[1], dataloader_list[2]

        train_sort_idx, val_sort_idx, test_sort_idx = get_order_data(params)
        train_path = ws + f'/data/dataset/{params["dataset"]}/train.npy'
        val_path = ws + f'/data/dataset/{params["dataset"]}/val.npy'
        test_path = ws + f'/data/dataset/{params["dataset"]}/test.npy'
        train_data = np.load(train_path, allow_pickle=True).item()
        val_data = np.load(val_path, allow_pickle=True).item()
        test_data = np.load(test_path, allow_pickle=True).item()
        train_loader, val_loader, test_loader = get_dataloader([train_data, val_data, test_data], [train_sort_idx, val_sort_idx, test_sort_idx])

        model = RankEPTA(params)
        model.to(device)
        from algorithm.ranketpa.time_predictor import save2file
        result_dict = train_val_test(train_loader, val_loader, test_loader, model, device, process_batch_eta, test_model_eta, params, save2file)
        params = dict_merge([params, result_dict])

    return params

def get_params():
    # Training parameters
    from utils.util import get_common_params
    parser = get_common_params()
    parser.add_argument('--model', type=str, default='ranketpa')
    args, _ = parser.parse_known_args()
    return args

if __name__ == "__main__":
    import time, nni
    import logging
    logger = logging.getLogger('training')
    print(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    try:
        tuner_params = nni.get_next_parameter()
        logger.debug(tuner_params)
        params = vars(get_params())
        params.update(tuner_params)
        for data_set in ['delivery_cq']:
            params['dataset'] = data_set
            params['model_path'] = None # time predictor
            params['task'] = 'time_predict'  # select route_predict to obtain the route prediction results first
            params['cuda_id'] = 1
            params['hidden_size'] = 64

            main(params)
    except Exception as exception:
        logger.exception(exception)
        raise
