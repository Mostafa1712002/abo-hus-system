#!/usr/bin/env python3
"""Forced-align a known Arabic script to a clip's audio -> words.json.

Runs in venv-align (torch + ctc-forced-aligner), kept out of the pipeline venv so a
torch/numpy upgrade can never break the uploader. Called as a subprocess by
src/word_captions.py.

Usage: align_words.py clip.wav script.txt words.json [--lang ara] [--shift-ms -15]
"""
import argparse, json, re, sys
import torch
from ctc_forced_aligner import (load_audio, load_alignment_model, generate_emissions,
                                preprocess_text, get_alignments, get_spans,
                                postprocess_results)

ap = argparse.ArgumentParser()
ap.add_argument('audio'); ap.add_argument('script'); ap.add_argument('out')
ap.add_argument('--lang', default='ara')
# MMS onset lag measured on Arabic (videogenerator spikes): ~+12 ms -> pull back 15
ap.add_argument('--shift-ms', type=float, default=-15.0)
a = ap.parse_args()

raw = re.sub(r'[ً-ْٰـ]', '', open(a.script, encoding='utf-8').read())
clean = lambda w: re.sub(r'[^\w]', '', w, flags=re.UNICODE)
kept = [w for w in re.split(r'\s+', raw) if w.strip() and clean(w)]
if not kept:
    json.dump({'words': []}, open(a.out, 'w')); print('{"words":0}'); sys.exit(0)

model, tokenizer = load_alignment_model('cpu', dtype=torch.float32)
wave = load_audio(a.audio, model.dtype, model.device)
emissions, stride = generate_emissions(model, wave, batch_size=8)
tokens_starred, text_starred = preprocess_text(
    ' '.join(clean(w) for w in kept), romanize=True, language=a.lang)
segments, scores, blank = get_alignments(emissions, tokens_starred, tokenizer)
res = postprocess_results(text_starred, get_spans(tokens_starred, segments, blank),
                          stride, scores)

if len(res) != len(kept):
    print(f'[align] aligner returned {len(res)} for {len(kept)} script words', file=sys.stderr)

sh = a.shift_ms / 1000.0
words = [{'word': kept[i] if i < len(kept) else r['text'],
          'start': round(max(0.0, r['start'] + sh), 3),
          'score': round(float(r.get('score', 0)), 3)} for i, r in enumerate(res)]
# CTC ends words early - a caption line ends at the NEXT word's start, never at 'end'.
for i, w in enumerate(words):
    w['next_start'] = words[i + 1]['start'] if i + 1 < len(words) else None
json.dump({'words': words}, open(a.out, 'w'), ensure_ascii=False)
print(json.dumps({'words': len(words)}))
