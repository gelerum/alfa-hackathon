import numpy as np
import pandas as pd
from sklearn.cluster import KMeans

COLUMNS = ['size', 'valid', 'title', 'summary', 'link']

def summarize(news: pd.DataFrame, n_topics: int = 5) -> pd.DataFrame:
    # -> одна строка на тему, темы по убыванию важности для компании:
    #    size     сколько новостей в теме
    #    valid    средняя похожесть темы на компанию
    #    title    заголовок самой характерной новости темы
    #    summary  её анонс (генеративная версия сможет класть сюда свой пересказ)
    #    link     ссылка на неё
    # news — DataFrame из main.py с колонками embeding, valid, title, summary, link
    if news.empty:
        return pd.DataFrame(columns=COLUMNS)

    vectors = np.stack(news['embeding'].to_list())
    # вес новости = похожесть на компанию; отрицательные не должны тянуть центроид
    weights = news['valid'].to_numpy().clip(min=1e-6)

    # 1. делим новости на темы
    n_topics = min(n_topics, len(news))
    labels = KMeans(n_clusters=n_topics, n_init=10, random_state=0).fit_predict(vectors)

    topics = []
    for topic in range(n_topics):
        idx = np.flatnonzero(labels == topic)
        # 2. центроид темы, взвешенный по похожести на компанию
        centroid = np.average(vectors[idx], axis=0, weights=weights[idx])
        centroid /= np.linalg.norm(centroid)
        # 3. самая характерная новость темы — ближайшая к центроиду
        best = news.iloc[idx[np.argmax(vectors[idx] @ centroid)]]
        topics.append({
            'size': len(idx),
            'valid': news['valid'].iloc[idx].mean(),
            'title': best['title'],
            'summary': best['summary'],
            'link': best['link'],
        })
    # 4. сначала темы, важные для компании
    return (pd.DataFrame(topics, columns=COLUMNS)
            .sort_values('valid', ascending=False, ignore_index=True))
    