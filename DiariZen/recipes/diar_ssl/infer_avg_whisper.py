# Licensed under the MIT license.
# Adopted from https://github.com/espnet/espnet/blob/master/egs2/chime8_task1/diar_asr1/local/pyannote_diarize.py
# Copyright 2024 Brno University of Technology (author: Jiangyu Han, ihan@fit.vut.cz)

import toml 
from funasr import AutoModel
from modelscope.pipelines import pipeline
from modelscope.utils.constant import Tasks
import argparse
import os.path
from pathlib import Path
from pyannote.core import Segment
from pathlib import Path
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline
from pyannote.audio import Audio
import numpy as np
import torchaudio
from loguru import logger
from pyannote.metrics.segmentation import Annotation, Segment

from pyannote.audio.pipelines import SpeakerDiarization as SpeakerDiarizationPipeline
from pyannote.audio.utils.signal import Binarize


def scp2path(scp_file):
    """ return path list """
    lines = [line.strip().split()[1] for line in open(scp_file)]
    return lines

def split_maxlen(utt_group, min_len=10):
    # merge if
    out = []
    stack = []
    for utt in utt_group:
        if not stack or (utt.end - stack[0].start) < min_len:
            stack.append(utt)
            continue

        out.append(Segment(stack[0].start, stack[-1].end))
        stack = [utt]

    if len(stack):
        out.append(Segment(stack[0].start, stack[-1].end))

    return out

def merge_closer(annotation, delta=1.0, max_len=60, min_len=10):
    name = annotation.uri
    speakers = annotation.labels()
    new_annotation = Annotation(uri=name)
    for spk in speakers:
        c_segments = sorted(annotation.label_timeline(spk), key=lambda x: x.start)
        stack = []
        for seg in c_segments:
            if not stack or abs(stack[-1].end - seg.start) < delta:
                stack.append(seg)
                continue  # continue

            # more than delta, save the current max seg
            if (stack[-1].end - stack[0].start) > max_len:
                # break into parts of 10 seconds at least
                for sub_seg in split_maxlen(stack, min_len):
                    new_annotation[sub_seg] = spk
                stack = [seg]
            else:
                new_annotation[Segment(stack[0].start, stack[-1].end)] = spk
                stack = [seg]

        if len(stack):
            new_annotation[Segment(stack[0].start, stack[-1].end)] = spk

    return new_annotation
from pyannote.core import Segment

def get_text_with_timestamp(transcribe_res):
    timestamp_texts = []
    for item in transcribe_res:
        start = item.start
        end = item.end
        text = item.text.strip()
        timestamp_texts.append((Segment(start, end), text))
    return timestamp_texts


def add_speaker_info_to_text(timestamp_texts, ann):
    spk_text = []
    for seg, text in timestamp_texts:
        spk = 'SPEAKER_'+str(ann.crop(seg).argmax())
        spk_text.append((seg, spk, text))
    return spk_text


def merge_cache(text_cache):
    sentence = ''.join([item[-1] for item in text_cache])
    spk = text_cache[0][1]
    start = round(text_cache[0][0].start, 1)
    end = round(text_cache[-1][0].end, 1)
    return Segment(start, end), spk, sentence


PUNC_SENT_END = [',', '.', '?', '!', "，", "。", "？", "！"]


def merge_sentence(spk_text):
    merged_spk_text = []
    pre_spk = None
    text_cache = []
    for seg, spk, text in spk_text:
        if spk != pre_spk and pre_spk is not None and len(text_cache) > 0:
            merged_spk_text.append(merge_cache(text_cache))
            text_cache = [(seg, spk, text)]
            pre_spk = spk

        elif text and len(text) > 0 and text[-1] in PUNC_SENT_END:
            text_cache.append((seg, spk, text))
            merged_spk_text.append(merge_cache(text_cache))
            text_cache = []
            pre_spk = spk
        else:
            text_cache.append((seg, spk, text))
            pre_spk = spk
    if len(text_cache) > 0:
        merged_spk_text.append(merge_cache(text_cache))
    return merged_spk_text


def diarize_text(transcribe_res, diarization_result):
    timestamp_texts = get_text_with_timestamp(transcribe_res)
    spk_text = add_speaker_info_to_text(timestamp_texts, diarization_result)
    res_processed = merge_sentence(spk_text)
    return res_processed


def write_to_txt(spk_sent, file):
    with open(file, 'w') as fp:
        for seg, spk, sentence in spk_sent:
            line = f'{seg.start:.2f} {seg.end:.2f} {spk} {sentence}\n'
            fp.write(line)
def load_metric_summary(metric_file, ckpt_path):
    with open(metric_file, "r") as f:
        lines = f.readlines()
    out_lst = []
    for line in lines:
        assert "Validation Loss/DER" in line
        epoch = line.split()[4].split(':')[0]
        Loss, DER = line.split()[-3], line.split()[-1]
        bin_path = f"epoch_{str(epoch).zfill(4)}/pytorch_model.bin"
        out_lst.append({
            'epoch': int(epoch),
            'bin_path': ckpt_path / bin_path,
            'Loss': float(Loss),
            'DER': float(DER)
        })
    return out_lst


def diarize_session(
    sess_name,
    pipeline,
    wav_files,
    uem_boundaries=None,
    merge_closer_delta=1.5,
    max_length_merged=60,
    max_n_speakers=8,
):
    print('Extracting segmentations...')
    waveform, sample_rate = torchaudio.load(wav_files[0])
    segmentations = pipeline.get_segmentations({"waveform": waveform, "sample_rate": sample_rate}, soft=False)

    # binarize segmentation
    assert pipeline._segmentation.model.specifications.powerset is True
    binarized_segmentations = segmentations     # powerset

    # estimate frame-level number of instantaneous speakers
    count = pipeline.speaker_count(
        binarized_segmentations,
        pipeline._segmentation.model._receptive_field,
        warm_up=(0.0, 0.0),
    )

    print("Extracting Embeddings.")
    embeddings = pipeline.get_embeddings(
        {"waveform": waveform, "sample_rate": sample_rate},
        binarized_segmentations,
        exclude_overlap=pipeline.embedding_exclude_overlap,
    )

    #  shape: (num_chunks, local_num_speakers, dimension)
    print("Clustering.")
    hard_clusters, _, _ = pipeline.clustering(
        embeddings=embeddings,
        segmentations=binarized_segmentations,
        num_clusters=None,
        min_clusters=2,  # 4 for NSF
        max_clusters=max_n_speakers,  # max-speakers are ok
        file={
            "waveform": waveform, 
            "sample_rate": sample_rate
        },  # <== for oracle clustering
        frames=pipeline._segmentation.model._receptive_field,  # <== for oracle clustering
    )

    # during counting, we could possibly overcount the number of instantaneous
    # speakers due to segmentation errors, so we cap the maximum instantaneous number
    # of speakers by the `max_speakers` value
    count.data = np.minimum(count.data, max_n_speakers).astype(np.int8)

    # keep track of inactive speakers
    inactive_speakers = np.sum(binarized_segmentations.data, axis=1) == 0
    #   shape: (num_chunks, num_speakers)

    # reconstruct discrete diarization from raw hard clusters
    hard_clusters[inactive_speakers] = -2
    discrete_diarization, _ = pipeline.reconstruct(
        segmentations,
        hard_clusters,
        count,
    )

    # convert to annotation
    to_annotation = Binarize(
        onset=0.5,
        offset=0.5,
        min_duration_on=0.0,
        min_duration_off=pipeline.segmentation.min_duration_off
    )
    result = to_annotation(discrete_diarization)
    offset = uem_boundaries[0] / sample_rate
    new_annotation = Annotation(uri=sess_name)  # new annotation
    speakers = result.labels()
    for spk in speakers:
        for seg in result.label_timeline(spk):
            new_annotation[Segment(seg.start + offset, seg.end + offset)] = spk

    new_annotation = merge_closer(
        new_annotation, delta=merge_closer_delta, max_len=max_length_merged, min_len=10
    )
    return new_annotation


def read_uem(uem_file):
    with open(uem_file, "r") as f:
        lines = f.readlines()
    lines = [x.rstrip("\n") for x in lines]
    uem2sess = {}
    for x in lines:
        sess_id, _, start, stop = x.split(" ")
        uem2sess[sess_id] = (float(start), float(stop))
    return uem2sess


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "This script performs diarization using Pyannote audio diarization pipeline ",
        add_help=True,
        usage="%(prog)s [options]",
    )
    parser.add_argument(
        "-C",
        "--configuration",
        required=True,
        type=str,
        help="Configuration (*.toml).",
    )
    parser.add_argument(
        "-i,--in_wav_scp",
        type=str,
        help="test wav.scp.",
        metavar="STR",
        dest="in_wav_scp",
    )
    parser.add_argument(
        "-o,--out_folder",
        type=str,
        default="",
        required=False,
        help="Path to output folder.",
        metavar="STR",
        dest="out_dir",
    )
    parser.add_argument(
        "-u,--uem",
        type=str,
        default="",
        required=False,
        help="Path to uem file.",
        metavar="STR",
        dest="uem_file",
    )
    parser.add_argument(
        "--avg_ckpt_num",
        type=int,
        default=5,
        required=False,
        help="the number of chckpoints of model averaging",
        metavar="STR",
        dest="avg_ckpt_num",
    )
    parser.add_argument(
        "--val_metric",
        type=str,
        default="DER",
        required=False,
        help="validation metric",
        choices=["Loss", "DER"],
        metavar="STR",
        dest="val_metric",
    )
    parser.add_argument(
        "--val_mode",
        type=str,
        default="best",
        required=False,
        help="validation metric mode",
        choices=["best", "prev", "center"],
        metavar="STR",
        dest="val_mode",
    )
    parser.add_argument(
        "--val_metric_summary",
        type=str,
        default="",
        required=False,
        help="val_metric_summary",
        metavar="STR",
        dest="val_metric_summary",
    )
    parser.add_argument(
        "--segmentation_model",
        required=False,
        type=str,
        help="Pre-trained segmentation model used.",
        metavar="STR",
        dest="segmentation_model",
    )
    parser.add_argument(
        "--embedding_model",
        required=False,
        type=str,
        help="Pre-trained segmentation model used.",
        metavar="STR",
        dest="embedding_model",
    )
    parser.add_argument(
        "--max_speakers",
        type=int,
        default=8,
        help="Max number of speakers in each session.",
        metavar="INT",
        dest="max_speakers",
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        default=32,
        help="batch size used for segmentation and embeddings extraction.",
        metavar="INT",
        dest="batch_size",
    )
    parser.add_argument(
        "--max_length_merged",
        type=str,
        default="60",
        help="Max length of segments that will be merged together. "
        "Reduce to reduce GSS GPU memory occupation later in the recipe.",
        metavar="STR",
        dest="max_length_merged",
    )
    parser.add_argument(
        "--merge_closer",
        type=str,
        default="0.5",
        help="Merge segments from same speakers that "
        "are less than this value apart.",
        metavar="STR",
        dest="merge_closer",
    )
    parser.add_argument(
        "--cluster_threshold",
        type=float,
        default=0.75,
        help="cluster_threshold",
        metavar="STR",
        dest="cluster_threshold",
    )
    parser.add_argument(
        "--min_cluster_size",
        type=int,
        default=15,
        help="min_cluster_size",
        metavar="STR",
        dest="min_cluster_size",
    )
    parser.add_argument(
        "--segmentation_step",
        type=float,
        default=0.1,
        help="segmentation_step",
        metavar="STR",
        dest="segmentation_step",
    )


    args = parser.parse_args()
    print(args)

    config_path = Path(args.configuration).expanduser().absolute()
    config = toml.load(config_path.as_posix())
    
    PIPELINE_PARAMS = {
        "clustering": {
            "method": "centroid",
            "min_cluster_size": args.min_cluster_size,
            "threshold": args.cluster_threshold   # 0.7153814381597874,
        },
        "segmentation": {
            "min_duration_off": 0.0    # 0.5817029604921046,
        },
    }

    ckpt_path = config_path.parent / 'checkpoints'
    if args.val_metric_summary:
        val_metric_lst = load_metric_summary(args.val_metric_summary, ckpt_path)
        val_metric_lst_sorted = sorted(val_metric_lst, key=lambda i: i[args.val_metric])
        best_val_metric_idx = val_metric_lst.index(val_metric_lst_sorted[0])
        if args.val_mode == "best":
            # print(f'averaging the best {args.avg_ckpt_num} checkpoints...')
            segmentation = val_metric_lst_sorted[:args.avg_ckpt_num]
        elif args.val_mode == "prev":
            # print(f'averaging previous {args.avg_ckpt_num} checkpoints to the converged moment...')
            segmentation = val_metric_lst[
                best_val_metric_idx - args.avg_ckpt_num + 1 :
                best_val_metric_idx + 1
            ]
        else:
            # print(f'averaging {args.avg_ckpt_num} checkpoints around the converged moment...')
            segmentation = val_metric_lst[
                best_val_metric_idx - args.avg_ckpt_num // 2 :
                best_val_metric_idx + args.avg_ckpt_num // 2 + 1
            ]
        assert len(segmentation) == args.avg_ckpt_num
    else:
        segmentation = args.segmentation_model

    # create, instantiate and apply the pipeline
    diarization_pipeline = SpeakerDiarizationPipeline(
        config=config,      # model configurations 
        segmentation=segmentation,
        segmentation_step=args.segmentation_step,
        embedding=args.embedding_model,
        embedding_exclude_overlap=True,
        clustering="AgglomerativeClustering",
        embedding_batch_size=args.batch_size,
        segmentation_batch_size=args.batch_size
    )
    diarization_pipeline.instantiate(PIPELINE_PARAMS)
    path = '/home/guyf/DiariZen/recipes/faster-whisper-large-v3'

    # 测试音频： https://isv-data.oss-cn-hangzhou.aliyuncs.com/ics/MaaS/ASR/test_audio/asr_speaker_demo.wav
    # audio = "./test/asr/data/asr_speaker_demo.wav"
    asr_model = WhisperModel(path, device="cuda", compute_type="float16")

    Path(args.out_dir).mkdir(exist_ok=True, parents=True)

    audio_f = scp2path(args.in_wav_scp)

    if args.uem_file:
        uem_map = read_uem(args.uem_file)
        # joint diarization of all mics
        sess2audio = {}
        for audio_file in audio_f:
            sess_name = Path(audio_file).stem.split('.')[0]
            if sess_name not in sess2audio.keys():
                sess2audio[sess_name] = []
            sess2audio[sess_name].append(audio_file)


        # now for each session
        for sess in sess2audio.keys():
            print("Diarizing Session {}".format(sess))
            print(f'sess={sess}')
            if args.uem_file:
                c_uem = uem_map[sess]
            else:
                c_uem = None
            audio = sess2audio[sess][0]
            c_result = diarize_session(
                sess,
                diarization_pipeline,
                sess2audio[sess],
                c_uem,
                float(args.merge_closer),
                float(args.max_length_merged),
                args.max_speakers,
            )
            c_rttm_out = os.path.join(args.out_dir, sess + ".rttm")

            with open(c_rttm_out, "w") as f:
                f.write(c_result.to_rttm())

            out_dir = '/home/guyf/DiariZen/recipes/output_txt'
            asr_result, info = asr_model.transcribe(audio, language="en", beam_size=5)
            diarization_result = c_result
            final_result = diarize_text(asr_result, diarization_result)
            c_rttm_out = os.path.join(out_dir, sess + ".txt")
            #print(c_rttm_out)
            start=[]
            end=[]
            senten=[]

            with open(c_rttm_out, "w", encoding='utf-8') as f:
                for segment, spk, sent in final_result:
                    #print(f'spk{spk}')
                    if (segment, spk, sent) == final_result[0]:
                        speaker=spk
                        senten.append(sent)
                        sentence = sent
                        start.append(segment.start)
                        end.append(segment.end)

                    if spk==speaker and (segment, spk, sent) != final_result[0]:
                        if sent!=senten[-1]:
                            sentence+=(' '+str(sent))
                            senten.append(sent)
                        print(f'sentence={sentence}')
                        start.append(segment.start)
                        end.append(segment.end)

                    elif (segment, spk, sent) != final_result[0]:
                        print("[%.2fs -> %.2fs] %s %s" % (start[0], end[-1], sentence, speaker))
                        f.write("[%.2fs -> %.2fs] %s %s" % (start[0], end[-1], sentence, speaker))
                        f.write('\n')
                        speaker=spk
                        start=[]
                        end=[]
                        senten=[]
                        start.append(segment.start)
                        end.append(segment.end)
                        senten.append(sent)
                        sentence=sent

                    if (segment, spk, sent) == final_result[-1]:
                        print("[%.2fs -> %.2fs] %s %s" % (start[0], end[-1], sentence, speaker))
                        f.write("[%.2fs -> %.2fs] %s %s" % (start[0], end[-1], sentence, speaker))
                        f.write('\n')
            '''with open(c_rttm_out, "w", encoding='utf-8') as f:
                for segment, spk, sent in final_result:
                    
                    print("[%.2fs -> %.2fs] %s %s" % (segment.start, segment.end, sent, spk))
                    f.write("[%.2fs -> %.2fs] %s %s" % (segment.start, segment.end, sent, spk))
                    f.write('\n')'''

