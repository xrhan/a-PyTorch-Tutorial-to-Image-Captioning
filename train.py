import time
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence
from models import Encoder, DualEncoder, DecoderWithAttention
from datasets import *
from utils import *
from nltk.translate.bleu_score import corpus_bleu
from collections import OrderedDict
from caption import visualize_att, caption_image_beam_search
import random
import numpy as np

# Data parameters
#data_folder = 'nycc_out_captions'  # folder with data files saved by create_input_files.py
data_folder = 'nycc_out_good_vocab'
data_name = 'coco_5_cap_per_img_5_min_word_freq'  # base name shared by data files

# Model parameters
emb_dim = 512  # dimension of word embeddings
attention_dim = 512  # dimension of attention linear layers
decoder_dim = 512  # dimension of decoder RNN
dropout = 0.5
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")  # sets device for model and PyTorch tensors
cudnn.benchmark = True  # set to true only if inputs to model are fixed size; otherwise lot of computational overhead

# Training parameters
start_epoch = 0
epochs = 31  # number of epochs to train for (if early stopping is not triggered)
epochs_since_improvement = 0  # keeps track of number of epochs since there's been an improvement in validation BLEU
batch_size = 32
workers = 1  # for data-loading; right now, only 1 works with h5py
encoder_lr = 1e-4  # learning rate for encoder if fine-tuning
decoder_lr = 2e-4  # learning rate for decoder
grad_clip = 5.  # clip gradients at an absolute value of
alpha_c = 1.  # regularization parameter for 'doubly stochastic attention', as in the paper
best_bleu4 = 0.  # BLEU-4 score right now
print_freq = 25  # print training/validation stats every __ batches
fine_tune_encoder = True  # fine-tune encoder?
# checkpoint = None  # path to checkpoint, None if none
# checkpoint = 'BEST_description_pre_train_single.pth.tar'  # main branch checkpoint, it not dual, load entire model
checkpoint = 'good_vocab_BEST_checkpoint_coco_5_cap_per_img_5_min_word_freq.pth.tar'
main_encoder_resnet = None  # should use the pre-trained architecture
sketch_encoder_resnet = 'sketch_weights71_epoch7.pt'

dual_encoder = False
#dual_encoder_checkpoint = 'BEST_checkpoint_coco_5_cap_per_img_5_min_word_freq.pth.tar'
dual_encoder_checkpoint = 'dual_BEST_description_pre_train.pth.tar'


# Read word map
word_map_file = os.path.join(data_folder, 'WORDMAP_' + data_name + '.json')
with open(word_map_file, 'r') as j:
    word_map = json.load(j)
rev_word_map = {v: k for k, v in word_map.items()}  # ix2word

with open(f'nycc_out_good_vocab/train_imgs/train_path.json', 'r') as f:
    train_files_list = json.load(f)
    train_files_list = [f"nycc_out_good_vocab/train_imgs/{t}.jpg" for t in train_files_list]
with open(f'nycc_out_good_vocab/val_imgs/val_path.json', 'r') as f:
    val_files_list = json.load(f)
    val_files_list = [f"nycc_out_good_vocab/val_imgs/{t}.jpg" for t in val_files_list]


def run_samples(encoder, decoder, fs, n, path_prefix, word_map, rev_word_map):
    all_chosen = np.random.choice(len(fs), n)
    for i in all_chosen:
        f = fs[i]
        # Encode, decode with attention and beam search
        seq, alphas = caption_image_beam_search(encoder, decoder, f, word_map, 5)
        alphas = torch.FloatTensor(alphas)

        # Visualize caption and attention of best sequence
        visualize_att(f, seq, alphas, rev_word_map, f'{path_prefix}_{i}_result.png')


def main():
    """
    Training and validation.
    """

    global best_bleu4, epochs_since_improvement, checkpoint, start_epoch, fine_tune_encoder, data_name, word_map

    if dual_encoder:  # this is always initialized with pre-trained models:
        print("DUAL ENCODER")
        if dual_encoder_checkpoint is not None:
            print('Loaded Dual Encoder Checkpoint')
            dual_branch_checkpoint = torch.load(checkpoint, map_location='cuda:0')
            encoder = dual_branch_checkpoint['encoder']

            decoder = dual_branch_checkpoint['decoder']
            decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, decoder.parameters()),
                                             lr=decoder_lr)

        else:
            main_branch_checkpoint = torch.load(checkpoint, map_location='cuda:0')
            encoder = DualEncoder(sketch_resnet=sketch_encoder_resnet)
            encoder.m_resnet = main_branch_checkpoint['encoder'].resnet
            print("Use pre-trained resnet")
            # encoder.m_adaptive_pool = main_branch_checkpoint['encoder'].adaptive_pool

            decoder = main_branch_checkpoint['decoder']
            decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, decoder.parameters()),
                                                lr=decoder_lr)

        if fine_tune_encoder is True:
            print("!!! Will fine tune Encoder !!!")
            encoder.fine_tune(fine_tune_encoder)
            encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                                 lr=encoder_lr)

        else:
            encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                                 lr=encoder_lr)


    else:  # following method is for One Encoder architecture
        # Initialize / load checkpoint
        if checkpoint is None:
            decoder = DecoderWithAttention(attention_dim=attention_dim,
                                           embed_dim=emb_dim,
                                           decoder_dim=decoder_dim,
                                           vocab_size=len(word_map),
                                           dropout=dropout)
            decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, decoder.parameters()),
                                                 lr=decoder_lr)
            encoder = Encoder(specify_resnet=main_encoder_resnet)
            encoder.fine_tune(fine_tune_encoder)
            encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                                 lr=encoder_lr) if fine_tune_encoder else None

        else:
            checkpoint = torch.load(checkpoint, map_location='cuda:0')
            # start_epoch = checkpoint['epoch'] + 1
            # epochs_since_improvement = checkpoint['epochs_since_improvement']
            # best_bleu4 = checkpoint['bleu-4'] this metric is unfair when we switch to a different domain
            decoder = checkpoint['decoder']
            # decoder_optimizer = checkpoint['decoder_optimizer']
            decoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, decoder.parameters()),
                                                 lr=decoder_lr)
            if main_encoder_resnet is not None:
                encoder = Encoder(
                    specify_resnet=main_encoder_resnet)  # specify here so the encoder remove the last 2 layers of resnet
                encoder.adaptive_pool = checkpoint['encoder'].adaptive_pool

            else:
                encoder = checkpoint['encoder']

            # encoder_optimizer = checkpoint['encoder_optimizer']
            # if fine_tune_encoder is True and encoder_optimizer is None:

            if fine_tune_encoder is True:
                print("Will fine tune Encoder")
                encoder.fine_tune(fine_tune_encoder)
                encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                                     lr=encoder_lr)

            else:
                encoder_optimizer = torch.optim.Adam(params=filter(lambda p: p.requires_grad, encoder.parameters()),
                                                     lr=encoder_lr)

    # Move to GPU, if available
    decoder = decoder.to(device)
    encoder = encoder.to(device)

    # Loss function
    criterion = nn.CrossEntropyLoss().to(device)

    # Custom dataloaders
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    # data augmention for nycc dataset
    augment = transforms.Compose([
        transforms.RandomAffine(20, (0.1, 0.1), (0.8, 1.2)),
        transforms.RandomHorizontalFlip(p=0.5)])

    train_loader = torch.utils.data.DataLoader(
        CaptionDataset(data_folder, data_name, 'TRAIN', transform=transforms.Compose([augment, normalize])),
        batch_size=batch_size, shuffle=True, num_workers=workers, pin_memory=True)
    val_loader = torch.utils.data.DataLoader(
        CaptionDataset(data_folder, data_name, 'VAL', transform=transforms.Compose([normalize])),
        batch_size=batch_size, shuffle=True, num_workers=workers, pin_memory=True)

    # Epochs
    for epoch in range(start_epoch, epochs):

        # Decay learning rate if there is no improvement for 8 consecutive epochs, and terminate training after 20
        # if epochs_since_improvement == 40:
        #    break
        # if epochs_since_improvement > 0 and epochs_since_improvement % 8 == 0:
        #    adjust_learning_rate(decoder_optimizer, 0.8)
        #    if fine_tune_encoder:
        #        adjust_learning_rate(encoder_optimizer, 0.8)

        # One epoch's training
        train(train_loader=train_loader,
              encoder=encoder,
              decoder=decoder,
              criterion=criterion,
              encoder_optimizer=encoder_optimizer,
              decoder_optimizer=decoder_optimizer,
              epoch=epoch)

        # One epoch's validation
        recent_bleu4 = validate(val_loader=val_loader,
                                encoder=encoder,
                                decoder=decoder,
                                criterion=criterion, epoch=epoch)

        # Check if there was an improvement
        is_best = recent_bleu4 > best_bleu4
        best_bleu4 = max(recent_bleu4, best_bleu4)
        if not is_best:
            epochs_since_improvement += 1
            print("\nEpochs since last improvement: %d\n" % (epochs_since_improvement,))
        else:
            epochs_since_improvement = 0
            # Save checkpoint
            print(" *** saving model with bleu score: ", recent_bleu4)
            save_checkpoint(data_name, epoch, epochs_since_improvement, encoder, decoder, encoder_optimizer,
                            decoder_optimizer, recent_bleu4, is_best)

    print(" *** LAST EPOCH saving model with bleu score: ", recent_bleu4)
    save_checkpoint(data_name, epoch, epochs_since_improvement, encoder, decoder, encoder_optimizer,
                    decoder_optimizer, recent_bleu4, is_best)


def train(train_loader, encoder, decoder, criterion, encoder_optimizer, decoder_optimizer, epoch):
    """
    Performs one epoch's training.

    :param train_loader: DataLoader for training data
    :param encoder: encoder model
    :param decoder: decoder model
    :param criterion: loss layer
    :param encoder_optimizer: optimizer to update encoder's weights (if fine-tuning)
    :param decoder_optimizer: optimizer to update decoder's weights
    :param epoch: epoch number
    """

    decoder.train()  # train mode (dropout and batchnorm is used)
    encoder.train()

    batch_time = AverageMeter()  # forward prop. + back prop. time
    data_time = AverageMeter()  # data loading time
    losses = AverageMeter()  # loss (per word decoded)
    top5accs = AverageMeter()  # top5 accuracy

    start = time.time()

    # Batches
    for i, (imgs, caps, caplens) in enumerate(train_loader):
        data_time.update(time.time() - start)

        # Move to GPU, if available
        imgs = imgs.to(device)
        caps = caps.to(device)
        caplens = caplens.to(device)

        # Forward prop.
        imgs = encoder(imgs)
        scores, caps_sorted, decode_lengths, alphas, sort_ind = decoder(imgs, caps, caplens)

        # Since we decoded starting with <start>, the targets are all words after <start>, up to <end>
        targets = caps_sorted[:, 1:]

        # Remove timesteps that we didn't decode at, or are pads
        # pack_padded_sequence is an easy trick to do this
        scores = pack_padded_sequence(scores, decode_lengths, batch_first=True).data
        targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

        # Calculate loss
        loss = criterion(scores, targets)

        # Add doubly stochastic attention regularization
        loss += alpha_c * ((1. - alphas.sum(dim=1)) ** 2).mean()

        # Back prop.
        decoder_optimizer.zero_grad()
        if encoder_optimizer is not None:
            encoder_optimizer.zero_grad()
        loss.backward()

        # Clip gradients
        if grad_clip is not None:
            clip_gradient(decoder_optimizer, grad_clip)
            if encoder_optimizer is not None:
                clip_gradient(encoder_optimizer, grad_clip)

        # Update weights
        decoder_optimizer.step()
        if encoder_optimizer is not None:
            encoder_optimizer.step()

        # Keep track of metrics
        top5 = accuracy(scores, targets, 5)
        losses.update(loss.item(), sum(decode_lengths))
        top5accs.update(top5, sum(decode_lengths))
        batch_time.update(time.time() - start)

        start = time.time()

        # Print status
        if i % print_freq == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data Load Time {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Top-5 Accuracy {top5.val:.3f} ({top5.avg:.3f})'.format(epoch, i, len(train_loader),
                                                                          batch_time=batch_time,
                                                                          data_time=data_time, loss=losses,
                                                                          top5=top5accs))


def validate(val_loader, encoder, decoder, criterion, epoch):
    """
    Performs one epoch's validation.

    :param val_loader: DataLoader for validation data.
    :param encoder: encoder model
    :param decoder: decoder model
    :param criterion: loss layer
    :return: BLEU-4 score
    """
    decoder.eval()  # eval mode (no dropout or batchnorm)
    if encoder is not None:
        encoder.eval()

    batch_time = AverageMeter()
    losses = AverageMeter()
    top5accs = AverageMeter()

    start = time.time()

    references = list()  # references (true captions) for calculating BLEU-4 score
    hypotheses = list()  # hypotheses (predictions)

    # explicitly disable gradient calculation to avoid CUDA memory error
    # solves the issue #57
    with torch.no_grad():
        # Batches
        for i, (imgs, caps, caplens, allcaps) in enumerate(val_loader):

            # Move to device, if available
            imgs = imgs.to(device)
            caps = caps.to(device)
            caplens = caplens.to(device)

            # Forward prop.
            if encoder is not None:
                imgs = encoder(imgs)
            scores, caps_sorted, decode_lengths, alphas, sort_ind = decoder(imgs, caps, caplens)

            # Since we decoded starting with <start>, the targets are all words after <start>, up to <end>
            targets = caps_sorted[:, 1:]

            # Remove timesteps that we didn't decode at, or are pads
            # pack_padded_sequence is an easy trick to do this
            scores_copy = scores.clone()
            scores = pack_padded_sequence(scores, decode_lengths, batch_first=True).data
            targets = pack_padded_sequence(targets, decode_lengths, batch_first=True).data

            # Calculate loss
            loss = criterion(scores, targets)

            # Add doubly stochastic attention regularization
            loss += alpha_c * ((1. - alphas.sum(dim=1)) ** 2).mean()

            # Keep track of metrics
            losses.update(loss.item(), sum(decode_lengths))
            top5 = accuracy(scores, targets, 5)
            top5accs.update(top5, sum(decode_lengths))
            batch_time.update(time.time() - start)

            start = time.time()

            if i % print_freq == 0:
                print('Validation: [{0}/{1}]\t'
                      'Batch Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Top-5 Accuracy {top5.val:.3f} ({top5.avg:.3f})\t'.format(i, len(val_loader),
                                                                                batch_time=batch_time,
                                                                                loss=losses, top5=top5accs))

            # Store references (true captions), and hypothesis (prediction) for each image
            # If for n images, we have n hypotheses, and references a, b, c... for each image, we need -
            # references = [[ref1a, ref1b, ref1c], [ref2a, ref2b], ...], hypotheses = [hyp1, hyp2, ...]

            # References
            allcaps = allcaps[sort_ind]  # because images were sorted in the decoder
            for j in range(allcaps.shape[0]):
                img_caps = allcaps[j].tolist()
                img_captions = list(
                    map(lambda c: [w for w in c if w not in {word_map['<start>'], word_map['<pad>']}],
                        img_caps))  # remove <start> and pads
                references.append(img_captions)

            # Hypotheses
            _, preds = torch.max(scores_copy, dim=2)
            preds = preds.tolist()
            temp_preds = list()
            for j, p in enumerate(preds):
                temp_preds.append(preds[j][:decode_lengths[j]])  # remove pads
            preds = temp_preds
            hypotheses.extend(preds)

            assert len(references) == len(hypotheses)

        # Calculate BLEU-4 scores
        bleu4 = corpus_bleu(references, hypotheses)

        print(
            '\n * LOSS - {loss.avg:.3f}, TOP-5 ACCURACY - {top5.avg:.3f}, BLEU-4 - {bleu}\n'.format(
                loss=losses,
                top5=top5accs,
                bleu=bleu4))
        # Run on some examples
        # print(encoder.weights1)

    # print(f'run on examples after epoch {epoch}')
    # run_samples(encoder, decoder, train_files_list, 2, f'sample_out/train_epoch_{epoch}', word_map, rev_word_map)

    if epoch % 5 == 0:
        run_samples(encoder, decoder, train_files_list, 1, f'sample_out/train_epoch_{epoch}', word_map, rev_word_map)
        run_samples(encoder, decoder, val_files_list, 2, f'sample_out/val_epoch_{epoch}', word_map, rev_word_map)

    return bleu4


if __name__ == '__main__':
    main()