import os
import sys
import h5py
import pickle
import argparse
import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau
from model import DnCNN, DnCNN_Res
from utils import weights_init_kaiming
from tqdm import tqdm
from tensorboardX import SummaryWriter
from torchvision.utils import make_grid
from skimage.metrics import peak_signal_noise_ratio as psnr


class Dataset(torch.utils.data.Dataset):
    def __init__(self, file_name):
        super(Dataset, self).__init__()
        self.file_name = file_name
        with h5py.File(file_name, 'r') as data:
            self.keys = list(data.keys())
        np.random.shuffle(self.keys)

    def __len__(self):
        return len(self.keys)
    
    def __getitem__(self, index):
        with h5py.File(self.file_name, 'r') as data:
            example = np.array(data[self.keys[index]])
        return torch.Tensor(example)

    def shape(self):
        with h5py.File(self.file_name, 'r') as data:
            return np.array(data[self.keys[0]]).shape

        
def setup_gpus():
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    device_ids = [i for i in range(torch.cuda.device_count())]
    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(map(str, device_ids))
    return device_ids

def psnr_of_batch(clean_imgs, denoised_imgs):
    clean_imgs = clean_imgs.data.cpu().numpy().astype(np.float32)
    denoised_imgs = denoised_imgs.data.cpu().numpy().astype(np.float32)
    batch_psnr = 0
    for i in range(clean_imgs.shape[0]):
        batch_psnr += psnr(clean_imgs[i,:,:,:], denoised_imgs[i,:,:,:], data_range=1)
    return batch_psnr/clean_imgs.shape[0]

def main():

    parser = argparse. ArgumentParser(description='Image Denoising Trainer')
    parser.add_argument('--train_set', type=str, default='train.h5', help='h5 file with training vectors')
    parser.add_argument('--val_set', type=str, default='val.h5', help='h5 file with validation vectors')
    parser.add_argument('--batch_size', type=int, default=128, help='batch size for training')
    parser.add_argument('--epochs', type=int, default=80, help='number of epochs')
    parser.add_argument('--lr', type=float, default=0.01, help='learning rate')
    parser.add_argument('--num_layers', type=int, default=17, help='number of CNN layers in network')
    parser.add_argument('--num_filters', type=int, default=64, help='number of filters per CNN layer')
    parser.add_argument('--filter_size', type=int, default=3, help='size of filter for CNN layers')
    parser.add_argument('--stride', type=int, default=1, help='filter stride for CNN layers')
    parser.add_argument('--noise_level', type=float, default=25.0, help='noise level for training')
    parser.add_argument('--log_dir', type=str, default='logs', help='location of log files')
    parser.add_argument('--model_dir', type=str, default='models', help='location of log files')
    args = parser.parse_args()

    # noise level for training, must be normalized like the clean image
    noise_level = args.noise_level/255

    # make sure data files exist
    assert os.path.exists(args.train_set), f'Cannot find training vectors file {args.train_set}'
    assert os.path.exists(args.val_set), f'Cannot find validation vectors file {args.train_set}'

    # make sure output dirs exists
    os.makedirs(args.log_dir, exist_ok=True)
    os.makedirs(args.model_dir, exist_ok=True)

    print('Loading datasets')

    # load data for training
    train_data = Dataset(args.train_set)
    val_data = Dataset(args.val_set)

    print(f'Number of training examples: {len(train_data)}')
    print(f'Number of validation examples: {len(val_data)}')

    # create batched data loaders for model
    train_loader = DataLoader(dataset=train_data, num_workers=os.cpu_count(), batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(dataset=val_data, num_workers=os.cpu_count(), batch_size=args.batch_size, shuffle=False)

    # input shape for each example to network, NOTE: channels first
    num_channels, patch_height, patch_width = train_data.shape()

    print(f'Input shape to model forward will be: ({args.batch_size}, {num_channels}, {patch_height}, {patch_width})')

    # detect gpus and setup environment variables
    device_ids = setup_gpus()
    print(f'Cuda devices found: {[torch.cuda.get_device_name(i) for i in device_ids]}')

    # create model
    model = DnCNN_Res(num_channels=num_channels, patch_size=patch_height,  num_layers=args.num_layers, \
                  kernel_size=args.filter_size, stride=args.stride, num_filters=args.num_filters) 

    # move model to available gpus
    model = torch.nn.DataParallel(model, device_ids=device_ids).cuda()

    # setup loss and optimizer
    criterion = torch.nn.MSELoss(reduction='sum').cuda()
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)

    # data struct to track training and validation losses per epoch
    model_params = {'num_channels':num_channels, 'patch_size':patch_height, \
                    'num_layers':args.num_layers, 'kernel_size':args.filter_size,\
                    'stride':args.stride, 'num_filters':args.num_filters}
    history = {'model': model_params, 'train':[], 'val':[], 'psnr':[]}
    pickle.dump(history, open(os.path.join(args.log_dir, 'model.npy'), 'wb'))
    writer = SummaryWriter(args.log_dir)

    # schedulers
    scheduler = ReduceLROnPlateau(optimizer, 'min', verbose=True, patience=4)

    # Main training loop
    best_val_loss = 999
    for epoch in range(args.epochs):
        print(f'Starting epoch {epoch+1} with learning rate {optimizer.param_groups[0]["lr"]}')
        model.train()
        # iterate through batches of training examples
        epoch_train_loss = 0
        train_steps = 0

        for data in tqdm(train_loader):
            model.zero_grad()
            optimizer.zero_grad()

            # generate additive white noise from gaussian distribution
            noise = torch.FloatTensor(data.size()).normal_(mean=0, std=noise_level)

            # pack input and target it into torch variable
            clean_imgs = Variable(data.cuda()) 
            noisy_imgs = Variable((data + noise).cuda()) 
            noise = Variable(noise.cuda())

            # make predictions
            preds = model(noisy_imgs)

            # calculate loss
            loss = criterion(preds, noise)/noisy_imgs.size()[0]
            epoch_train_loss += loss.item()

            # backprop
            loss.backward()
            optimizer.step()

            train_steps += 1

        # start evaluation
        model.eval() 
        print('Validating Model')
        epoch_val_loss = 0
        epoch_val_psnr = 0
        val_steps = 0
        with torch.no_grad():
            for data in tqdm(val_loader):
                # generate additive white noise from gaussian distribution
                noise = torch.FloatTensor(data.size()).normal_(mean=0, std=noise_level)

                # pack input and target it into torch variable
                clean_imgs = Variable(data.cuda()) 
                noisy_imgs = Variable((data + noise).cuda()) 
                noise = Variable(noise.cuda())

                # make predictions
                preds = model(noisy_imgs)

                # calculate loss
                val_loss = criterion(preds, noise)/noisy_imgs.size()[0]
                epoch_val_loss += val_loss.item()

                # calculate PSNR 
                denoised_imgs = torch.clamp(noisy_imgs-preds, 0.0, 1.0)
                epoch_val_psnr += psnr_of_batch(clean_imgs, denoised_imgs)


                val_steps += 1

        # epoch summary
        epoch_train_loss /= train_steps
        epoch_val_loss /= val_steps
        epoch_val_psnr /= val_steps

        # save if best model
        if epoch_val_loss < best_val_loss:
            print('Saving best model')
            best_val_loss = epoch_val_loss
            torch.save(model, os.path.join(args.log_dir, 'best_model.pt'))
            pickle.dump(history, open(os.path.join(args.log_dir, 'best_model.npy'), 'wb'))

        # reduce learning rate if validation has leveled off
        scheduler.step(epoch_val_loss)

        # save epoch stats
        history['train'].append(epoch_train_loss)
        history['val'].append(epoch_val_loss)
        history['psnr'].append(epoch_val_psnr)
        print(f'Training loss: {epoch_train_loss}')
        print(f'Validation loss: {epoch_val_loss}')
        print(f'Validation PSNR: {epoch_val_psnr}')
        writer.add_scalar('loss', epoch_train_loss, epoch)
        writer.add_scalar('val', epoch_val_loss, epoch)
        writer.add_scalar('PSNR', epoch_val_psnr, epoch)

        # test model and save results 
        if epoch % 5 == 0:
            denoised_imgs = torch.clamp(noisy_imgs-preds, 0.0, 1.0)
            clean_imgs = make_grid(clean_imgs.data, nrow=8, normalize=True, scale_each=True)
            noisy_imgs = make_grid(noisy_imgs.data, nrow=8, normalize=True, scale_each=True)
            denoised_imgs = make_grid(denoised_imgs.data, nrow=8, normalize=True, scale_each=True)
            writer.add_image('clean images', clean_imgs, epoch)
            writer.add_image('noisy images', noisy_imgs, epoch)
            writer.add_image('denoised images', denoised_imgs, epoch)

    # saving final model
    print('Saving final model')
    torch.save(model, os.path.join(args.log_dir, 'final_model.pt'))
    pickle.dump(history, open(os.path.join(args.log_dir, 'final_model.npy'), 'wb'))

    return 0

if __name__ == '__main__':
    sys.exit(main())
