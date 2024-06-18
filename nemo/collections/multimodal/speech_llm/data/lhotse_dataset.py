import torch.utils.data
import random
from lhotse import CutSet
from lhotse.dataset import AudioSamples
from lhotse.dataset.collation import collate_vectors as collate_vectors_lhotse
from lhotse.dataset.collation import _read_features

from nemo.collections.multimodal.speech_llm.parts.utils.data_utils import (
    TextProcessing,
    build_loss_mask,
    ceil_to_nearest,
)


def collate_vectors(items, max_length: int, padding_value):
    vectors = collate_vectors_lhotse(items, padding_value=padding_value)
    if max_length > vectors.size(1):
        vectors = torch.cat(
            [vectors, padding_value * torch.ones(vectors.size(0), max_length - vectors.size(1), dtype=vectors.dtype)],
            dim=1,
        )
    if items[0].shape[0] < 1:
        vectors = vectors.long()
    return vectors


class LhotseAudioQuestionAnswerDataset(torch.utils.data.Dataset):
    """
    This dataset is based on Lhotse ASR dataset from ``audio_to_text_lhotse.py``
    and ``TarredAudioQuestionAnswerDataset`` from ``audio_text_qa_dataset.py``.

    Unlike native NeMo datasets, Lhotse dataset defines only the mapping from
    a CutSet (meta-data) to a mini-batch with PyTorch tensors.
    Specifically, it performs tokenization, I/O, augmentation, and feature extraction (if any).
    Managing data, sampling, de-duplication across workers/nodes etc. is all handled
    by Lhotse samplers instead.

    Args:
        text_processor: TextProcessing object
        default_context: Default question to use if no question is provided
        tokens_to_generate: Number of tokens to generate during inference
        pad_to_max_length: Whether to pad the input to the max sequence length. If False, will pad to the max length of the current batch.
        max_seq_length: Maximum sequence length for each dataset examples. Examples will either be truncated to fit this length or dropped if they cannot be truncated.
        context_key: Key to use for the context in your JSONL file
        default_context_key: Key to use for the default context in lhotse yaml
    """

    def __init__(
        self,
        text_processor: TextProcessing,
        default_context: str,
        tokens_to_generate: int,
        pad_to_max_length: bool,
        max_seq_length: int,
        context_key: str = "context",
        default_context_key: str = "default_context",
    ):
        super().__init__()
        self.text_processor = text_processor
        self.load_audio = AudioSamples(fault_tolerant=True)
        self.tokens_to_generate = tokens_to_generate
        self.pad_to_max_length = pad_to_max_length
        self.max_seq_length = max_seq_length

        self.default_context = default_context
        self.context_key = context_key
        self.default_context_key = default_context_key

    def __getitem__(self, cuts) -> dict[str, torch.Tensor | list[str] | dict]:
        cuts = cuts.sort_by_duration()
        cuts_tts = []
        cuts_asr = []

        for cut in cuts:
            try:
                if getattr(cut, "tts"):
                    cuts_tts.append(cut)
                else:
                    cuts_asr.append(cut)
            except AttributeError:
                cuts_asr.append(cut)

        cuts = CutSet(cuts_asr)
        cuts_tts = CutSet(cuts_tts)
        print(f"Len_asr: {len(cuts)}")
        print(f"Len_tts: {len(cuts_tts)}")
        return_batch = {}

        if len(cuts) > 0:
            audio, audio_lens, cuts = self.load_audio(cuts)

            audio_ratio = []
            for id, cut in enumerate(cuts):
                audio_ratio.append(1.0)

            for _, cut in enumerate(cuts):
                if hasattr(cut, self.context_key):
                    cut.context = getattr(cut, self.context_key)
                elif hasattr(cut, self.default_context_key):
                    cut.context = getattr(cut, self.default_context_key)
                else:
                    cut.context = self.default_context

            metadata = []
            for id, cut in enumerate(cuts):
                metadata.append({'audio_filepath': cut.id + '.wav'})

            collated_text_data = collate_text_data(
                cuts=cuts,
                default_context=self.default_context,
                text_processor=self.text_processor,
                tokens_to_generate=self.tokens_to_generate,
                pad_to_max_length=self.pad_to_max_length,
                max_seq_length=self.max_seq_length,
            )
            # collate_text_data returns 4 fields:
            #   - tokens: context + answer; not used in T5 model
            #   - labels: tokens rotated; not used in T5 model
            #   - answers: Gets sent to decoder in T5 model
            #   - context: Gets sent to encoder in T5 model
            asr_batch = {
                "sample_ids": list(cuts.ids),
                "audio_signal": audio,
                "audio_signal_length": audio_lens,
                "audio_ratio": torch.FloatTensor(audio_ratio),
                "metadata": metadata,
                **collated_text_data,
            }

        # Now handle TTS if any
        if len(cuts_tts) > 0:
            # handle text data
            tts_text_data = [
                {
                    k: torch.as_tensor(v)
                    for k, v in self.text_processor._process_example(
                        context=cut.supervisions[0].text, output=""
                    ).items()
                }
                for cut in cuts_tts
            ]
            tts_text_data = as_dict(tts_text_data)
            # max_length = self.tokens_to_generate + get_max_len(tts_text_data["context_ids"])
            # if self.pad_to_max_length:
            #     max_length = self.max_seq_length
            # else:
            #     max_length = min(self.max_seq_length, ceil_to_nearest(max_length, 8))
            # tts_text_data = collate_vectors(tts_text_data["context_ids"], max_length=max_length, padding_value=pad_id)

            # Build answer and label tensor
            features_lens = torch.tensor([cut.num_frames for cut in cuts_tts], dtype=torch.int)
            tts_answer = torch.zeros(len(cuts_tts), max(features_lens).item(), 8) + 1001  # 1001 for speech pad_id
            # Loop through cuts and build tts_answer, label, and context tensors
            speaker_context_list = []
            for i, cut_t in enumerate(cuts_tts):
                feat_i = cut_t.load_features()
                tts_answer[i,:feat_i.shape[0],:] = torch.tensor(feat_i)
                speaker_context = cut_t.load_context()
                # take random 3s splice from context
                # TODO: fix hardcode
                rng = random.Random()  # Custom random generator (since random uses fixed seeds). Else context remains fixed
                reference_codec_len = 3 * 86
                reference_codec_len = min(reference_codec_len, speaker_context.shape[0])
                si = rng.randint(0, speaker_context.shape[0] - reference_codec_len)
                speaker_context = speaker_context[si : si + reference_codec_len, :]
                speaker_context_list.append(torch.tensor(speaker_context))
            tts_answer = tts_answer.to(torch.int)
            speaker_context = torch.stack(speaker_context_list)

        def get_max_len(input_list):
            return max([len(x) for x in input_list])
        import ipdb; ipdb.set_trace()

        pad_id = self.text_processor.pad_id
        if len(cuts) > 0 and len(cuts_tts) > 0:
            all_text_data = asr_batch["context_ids"] + tts_batch["tts_text_data"]
            # TODO: Mask out tts pad_id
            bos_tensor = torch.zeros([len(cuts_tts), 1, 8])
            bos_tensor[:,:,0] = self.text_processor.bos_id
            speech_token_offset = self.text_processor.tokenizer.vocab_size
            for i in range(speaker_context.shape(-1)):
                speaker_context[:,:,i] += speech_token_offset + i*1024
                tts_answer[:,:,i] += speech_token_offset + i*1024
            tts_decoder_in = torch.concat([speaker_context, bos_tensor, tts_answer], 1)
            asr_answer = asr_batch["answers"]
            asr_answer_padded = torch.nn.functional.pad(asr_answer, [tts_decoder_in.shape[1]-asr_answer.shape[1]],pad_id)
            answers = None  #TODO
            loss_mask = None  #TODO
        elif len(cuts) > 0:
            # Just asr data
            all_text_data = asr_batch["context_ids"]
            answers = asr_batch["answers"]
            loss_mask = asr_batch["loss_mask"]
        else:
            # Just tts data
            all_text_data = tts_batch["tts_text_data"]
            bos_tensor = torch.zeros([len(cuts_tts), 1, 8])
            bos_tensor[:,:,0] = self.text_processor.bos_id
            speech_token_offset = self.text_processor.tokenizer.vocab_size
            for i in range(speaker_context.shape(-1)):
                speaker_context[:,:,i] += speech_token_offset + i*1024
                tts_answer[:,:,i] += speech_token_offset + i*1024
            answers = torch.concat([speaker_context, bos_tensor, tts_answer], 1)
            # Move wav_tokens above current text token range

            loss_mask = None  #Need to mask out speaker_context potion of audio
        max_length = self.tokens_to_generate + get_max_len(all_text_data)
        if self.pad_to_max_length:
            max_length = self.max_seq_length
        else:
            max_length = min(self.max_seq_length, ceil_to_nearest(max_length, 8))
        contexts = collate_vectors(all_text_data, max_length=max_length, padding_value=pad_id)
        context_lengths = torch.LongTensor([len(seq) for seq in all_text_data])

        # Merge batch
        return_batch ={
            "audio_signal": audio,
            "audio_signal_length": audio_lens,
            "contexts": contexts,
            "context_lengths": context_length,
            "answers": answers,
            "loss_mask": loss_mask,
        }
        return return_batch


def collate_text_data(
    cuts,
    default_context: str,
    text_processor: TextProcessing,
    tokens_to_generate: int,
    pad_to_max_length: bool,
    max_seq_length: int,
) -> dict:
    """Perform text collation equivalent to nemo/collections/multimodal/data/audio_text_qa_dataset.py:121"""
    batch_size = len(cuts)
    pad_id = text_processor.pad_id
    examples = [
        {
            k: torch.as_tensor(v)
            for k, v in text_processor._process_example(
                context=cut.context,
                output=cut.supervisions[0].text,
            ).items()
        }
        for cut in cuts
    ]
    fields = as_dict(examples)

    def get_max_len(input_list):
        return max([len(x) for x in input_list])

    max_length = tokens_to_generate + max(
        get_max_len(fields["input_ids"]), get_max_len(fields["context_ids"]), get_max_len(fields["answer_ids"])
    )
    # increase max length to nearest multiple of 4 or 8
    if pad_to_max_length:
        max_length = max_seq_length
    else:
        max_length = min(max_seq_length, ceil_to_nearest(max_length, 8))

    all_tokens = collate_vectors(fields["input_ids"], max_length=max_length, padding_value=pad_id)
    full_lengths = torch.LongTensor([len(item) for item in fields["input_ids"]])

    assert max_length <= max_seq_length, f"{max_length=} <= {max_seq_length=}"

    return {
        "tokens": all_tokens[:, :-1],
        "tokens_length": full_lengths - 1,
        "labels": all_tokens[:, 1:],
        "loss_mask": collate_vectors(
            [torch.as_tensor(build_loss_mask(item)) for item in examples], max_length=max_length, padding_value=0
        )[:, 1:],
        "position_ids": torch.arange(max_length, dtype=torch.long).repeat(batch_size, 1),
        "contexts": collate_vectors(fields["context_ids"], max_length=max_length, padding_value=pad_id),
        "context_lengths": torch.LongTensor([len(seq) for seq in fields["context_ids"]]),
        "answers": collate_vectors(fields["answer_ids"], max_length=max_length, padding_value=pad_id),
        "max_length": torch.LongTensor([max_length] * batch_size),
        "context_ids": fields["context_ids"]
    }


def as_dict(arg: list[dict]) -> dict[str, list]:
    return {k: [item[k] for item in arg] for k in arg[0].keys()}
