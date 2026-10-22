import numpy as np
import spacy
from spacy_cleaner import processing
from spacy_cleaner.processing.removers import remove_punctuation_token
from tqdm.auto import tqdm

from sentence_transformers import SentenceTransformer


DEFAULT_MODEL = "deepvk/USER-bge-m3"
LIGHT_MODEL = "cointegrated/rubert-tiny2"  # в разы быстрее, но качество ниже
# Длинные статьи обрезаются до 512 подслов: так на ~20% быстрее,
# а вектор почти не меняется (косинус с полным текстом >= 0.91)
MAX_SEQ_LENGTH = 512

print('Загружаю spaCy...')
# parser и ner для очистки не нужны, без них быстрее, результат тот же
MODEL_FOR_CLEAN = spacy.load("ru_core_news_sm", disable=["parser", "ner"])
PROCESSORS = (
    processing.remove_stopword_token,
    remove_punctuation_token,
    processing.mutate_lemma_token,
)

# Модель эмбеддингов грузится в load_model(), а если её не вызвали —
# при первом tokenise/embed грузится DEFAULT_MODEL.
MODEL_BERT = None
TOKENIZER = None

def load_model(name:str = DEFAULT_MODEL) -> None:
    global MODEL_BERT, TOKENIZER
    print(f'Загружаю модель {name}...')
    MODEL_BERT = SentenceTransformer(name)
    if not MODEL_BERT.max_seq_length or MODEL_BERT.max_seq_length > MAX_SEQ_LENGTH:
        MODEL_BERT.max_seq_length = MAX_SEQ_LENGTH
    TOKENIZER = MODEL_BERT.tokenizer

def _model() -> SentenceTransformer:
    if MODEL_BERT is None:
        load_model()
    return MODEL_BERT

def clean(text:str) -> str:
    text = text.lower()
    return processing.clean_doc(MODEL_FOR_CLEAN(text), *PROCESSORS)

def tokenise(text:str) -> list:
    # text = "Центробанк снизил ключевую ставку до 15%!"
    text = clean(text)
    # -> "центробанк снизить ключевой ставка 15"    # str: леммы через пробел, без стоп-слов и пунктуации
    return _model().tokenizer.tokenize(text)
    # -> ['▁центр', 'о', 'банк', '▁снизить', '▁ключ', 'евой', '▁ставка', '▁15']    # list[str]: подслова (для USER-bge-m3)

def embed(tokens:list) -> np.ndarray:
    # tokens = tokenise("Центробанк снизил ключевую ставку до 15%!")
    text = _model().tokenizer.convert_tokens_to_string(tokens)
    # -> "центробанк снизить ключевой ставка 15"    # str: подслова склеены обратно
    # normalize_embeddings: длина вектора = 1 у любой модели, тогда скалярное произведение = косинус
    return MODEL_BERT.encode(text, normalize_embeddings=True)
    # -> array([0.017, -0.040, ..., -0.018], dtype=float32)    # один вектор "смысла" на весь текст,
    #                                                           # shape (1024,) у USER-bge-m3, (312,) у rubert-tiny2

def embed_many(tokens_list:list) -> np.ndarray:
    # tokens_list = [tokenise(text1), tokenise(text2), ...]
    # По одному: на CPU это быстрее, чем пачкой (замерено). Если появится GPU,
    # здесь можно перейти на MODEL_BERT.encode(texts, batch_size=...).
    dim = _model().get_embedding_dimension()
    vectors = [embed(tokens) for tokens in tqdm(tokens_list, desc='Эмбеддинги')]
    return np.stack(vectors) if vectors else np.empty((0, dim), dtype=np.float32)
    # -> array([[0.017, ..., -0.018],    # вектор текста 1
    #           [0.021, ...,  0.004],    # вектор текста 2
    #           ...], dtype=float32)     # shape (len(tokens_list), 1024)

# Пример:
# embed(tokenise("Центробанк снизил ключевую ставку до 15%!"))
# -> array([0.017, -0.040, ..., -0.018], dtype=float32)    # длина вектора = 1,
#                                                           # поэтому похожесть текстов = скалярное произведение
