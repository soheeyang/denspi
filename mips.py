from collections import namedtuple
import time

import h5py
import numpy as np


def int8_to_float(num, offset, factor):
    return num.astype(np.float32) / factor + offset


class DocumentPhraseMIPS(object):
    def __init__(self, document_index, phrase_index, max_answer_length, doc_score_cf):
        super(DocumentPhraseMIPS, self).__init__()
        self.document_index = document_index
        self.phrase_index = phrase_index
        self.max_answer_length = max_answer_length
        self.doc_score_cf = doc_score_cf

    def search_document(self, query, top_k=5):
        res = query * self.document_index

        if len(res.data) <= top_k:
            o_sort = np.argsort(-res.data)
        else:
            o = np.argpartition(-res.data, top_k)[0:top_k]
            o_sort = o[np.argsort(-res.data[o])]

        doc_indices = res.indices[o_sort]
        doc_scores = res.data[o_sort]
        out = [{'doc_idx': doc_idx, 'doc_score': doc_score}
               for doc_idx, doc_score in zip(doc_indices.tolist(), doc_scores.tolist())]
        return out

    def search_phrase(self, doc_idx, query, top_k=5, doc_score=0.0, para_idx=None):
        t0 = time.time()

        if str(doc_idx) not in self.phrase_index:
            return []

        group = self.phrase_index[str(doc_idx)]
        if para_idx is not None:
            group = group[str(para_idx)]

        start, end, span_logits, start2end, word2char_start, word2char_end = [
            group[key][:] for key in
            ['start', 'end', 'span_logits', 'start2end', 'word2char_start', 'word2char_end']]

        # print(start.min(), start.max(), end.min(), end.max())

        context = group.attrs['context']
        title = group.attrs['title']
        t1 = time.time()
        # print('Loading index: %dms' % int(1000 * (t1 - t0)))

        if 'offset' in group.attrs:
            start = int8_to_float(start, group.attrs['offset'], group.attrs['scale'])
            end = int8_to_float(end, group.attrs['offset'], group.attrs['scale'])

        query_start, query_end, query_span_logit = query
        start_scores = np.matmul(start, np.array(query_start).transpose()).squeeze(1)
        end_scores = np.matmul(end, np.array(query_end).transpose()).squeeze(1)
        query_span_logit = float(query_span_logit[0][0])
        t2 = time.time()
        # print('Computing IP: %dms' % int(1000 * (t2 - t1)))

        PhraseResult = namedtuple('PhraseResult', ('score', 'start_score', 'end_score', 'span_score',
                                                   'start_idx', 'end_idx'))
        results = []
        if top_k > 0:
            best_start_pairs = sorted(enumerate(start_scores.tolist()), key=lambda item: -item[1])[:top_k]
        else:
            best_start_pairs = enumerate(start_scores.tolist())
        end_scores = end_scores.tolist()
        for start_idx, start_score in best_start_pairs:
            max_result = None
            max_score = -1e9
            for i, end_idx in enumerate(start2end[start_idx]):
                if i >= self.max_answer_length:
                    break
                if end_idx < 0:
                    continue
                span_logit = span_logits[start_idx, i].item()
                end_score = end_scores[end_idx]
                span_score = query_span_logit * span_logit
                score = self.doc_score_cf * doc_score + start_score + end_score + span_score
                if score > max_score:
                    max_result = PhraseResult(score, start_score, end_score, span_score, start_idx, end_idx)
                    max_score = score
            if max_result is not None:
                results.append(max_result)
        results = sorted(results, key=lambda item: -item.score)

        # Non-maximal suppression (might not be needed)
        new_results = []
        for result in results:
            include = True
            for new_result in new_results:
                if overlap(word2char_start, word2char_end,
                           result.start_idx, result.end_idx, new_result.start_idx, new_result.end_idx):
                    include = False
                    break
            if include:
                new_results.append(result)
        results = new_results[:top_k]
        out = [{'context': context,
                'title': title,
                'doc_idx': doc_idx,
                'doc_score': doc_score,
                'start_pos': word2char_start[result.start_idx].item(),
                'end_pos': word2char_end[result.end_idx].item(),
                'start_score': result.start_score,
                'end_score': result.end_score,
                'span_score': result.span_score,
                'phrase_score': result.score - doc_score,
                'score': result.score} for result in results]
        t3 = time.time()
        # print('Finding answer: %dms' % int(1000 * (t3 - t2)))
        out = [adjust(each) for each in out]
        out = [each for each in out if len(each['context']) > 100 and each['score'] >= 30]

        for each in out:
            print(each['title'])
            print(each['context'][each['start_pos']:each['end_pos']])
            print(each['score'], each['start_score'], each['end_score'], each['span_score'], each['doc_score'])
            print()

        return out

    def search(self, doc_query, phrase_query, top_k_docs=5, top_k_phrases=5):
        document_rets = self.search_document(doc_query, top_k_docs)
        phrase_rets = sum([self.search_phrase(ret['doc_idx'], phrase_query, top_k=top_k_phrases,
                                              doc_score=ret['doc_score'])
                           for ret in document_rets], [])
        phrase_rets = sorted(phrase_rets, key=lambda ret: -ret['score'])[:top_k_phrases]
        return phrase_rets


def adjust(each):
    last = each['context'].rfind(' [PAR] ', 0, each['start_pos'])
    last = 0 if last == -1 else last + len(' [PAR] ')
    next = each['context'].find(' [PAR] ', each['end_pos'])
    next = len(each['context']) if next == -1 else next
    each['context'] = each['context'][last:next]
    each['start_pos'] -= last
    each['end_pos'] -= last
    return each


def overlap(t1, t2, a1, a2, b1, b2):
    if t1[b1] > t2[a2] or t1[a1] > t2[b2]:
        return False
    return True
