"""
Streamlit web UI for semantic search across Telegram & Instagram.
"""
import streamlit as st
import sys
import os
import re
import warnings
warnings.filterwarnings("ignore", message=".*torch.classes.*")
sys.path.insert(0, os.path.dirname(__file__))

from search_engine import search, generate_answer, get_stats, list_channels

st.set_page_config(
    page_title="🔍 Семантичний пошук",
    page_icon="🔍",
    layout="wide"
)

st.title("🔍 Семантичний пошук")
st.caption("Telegram · Instagram — пошук за змістом і контекстом, а не лише за ключовими словами")

# Sidebar with stats and settings
with st.sidebar:
    st.header("⚙️ Налаштування")

    source = st.selectbox(
        "Джерело",
        ["all", "telegram", "instagram"],
        format_func=lambda x: {"all": "🌐 Всі джерела", "telegram": "📱 Telegram", "instagram": "📸 Instagram"}[x]
    )

    channels = list_channels()
    channel_names = [c["name"] for c in channels]
    channel_labels = {c["name"]: f"{c['name']} ({c['count']:,})" for c in channels}

    if channel_names:
        col_a, col_b = st.columns(2)
        if col_a.button("✅ Всі", use_container_width=True):
            st.session_state["selected_channels"] = channel_names
        if col_b.button("🗑️ Очистити", use_container_width=True):
            st.session_state["selected_channels"] = []

        selected_channels = st.multiselect(
            "Канали для пошуку",
            options=channel_names,
            default=st.session_state.get("selected_channels", channel_names),
            format_func=lambda x: channel_labels.get(x, x),
        )
        st.session_state["selected_channels"] = selected_channels
    else:
        selected_channels = []
        st.info("Канали ще не проіндексовані. Запустіть скрипт індексації.")

    n_results = st.slider("Кількість результатів", 3, 20, 15)

    use_llm = st.toggle("🤖 Генерувати відповідь (Azure OpenAI)", value=True)

    answer_lang = st.selectbox(
        "Мова відповіді",
        ["uk", "en", "ru", "pl"],
        index=0,
        format_func=lambda x: {"en": "🇬🇧 Англійська", "uk": "🇺🇦 Українська", "ru": "🇷🇺 Російська", "pl": "🇵🇱 Польська"}[x]
    )

    st.divider()
    st.header("📊 Статистика")
    stats = get_stats()
    col1, col2 = st.columns(2)
    col1.metric("📱 Telegram", stats["telegram"])
    col2.metric("📸 Instagram", stats["instagram"])
    st.metric("📄 Всього документів", stats["total"])

    st.divider()
    st.caption("Для індексації нових даних:")
    st.code("source .venv/bin/activate\npython src/ingest_telegram.py", language="bash")
    st.code("source .venv/bin/activate\npython src/ingest_instagram.py", language="bash")

# Main search interface
query = st.text_input(
    "Задайте питання",
    placeholder="Наприклад: Де знайти оренду житла у Варшаві?",
    label_visibility="collapsed"
)

if query:
    if not selected_channels:
        st.warning("Будь ласка, оберіть хоча б один канал для пошуку.")
        st.stop()

    with st.spinner("🔎 Шукаємо..."):
        results = search(
            query=query,
            n_results=n_results,
            source_filter=source if source != "all" else None,
            channel_filter=selected_channels,
        )

    if not results:
        st.warning("Нічого не знайдено. Спробуйте переформулювати запит або переконайтесь, що індексація була виконана.")
    else:
        # LLM Answer
        if use_llm:
            with st.spinner("🤖 Генеруємо відповідь через Azure OpenAI..."):
                answer = generate_answer(query, results, language=answer_lang)
            # Replace [Source N] with clickable markdown links
            source_urls = {i: r.get("url") for i, r in enumerate(results[:7], 1)}
            def _linkify_source(m):
                n = int(m.group(1))
                url = source_urls.get(n)
                return f"[[Source {n}]]({url})" if url else m.group(0)
            answer = re.sub(r"\[Source (\d+)\]", _linkify_source, answer)
            st.markdown("### 💡 Відповідь")
            st.markdown(answer)
            st.divider()

        # Raw results
        st.markdown(f"### 📄 Знайдено {len(results)} релевантних повідомлень")

        for i, r in enumerate(results, 1):
            source_emoji = "📱" if r["source"] == "telegram" else "📸"
            similarity_pct = f"{r['similarity']*100:.1f}%"

            with st.expander(
                f"{source_emoji} {r['channel']}"
                + (f" → {r['topic']}" if r.get('topic') else "")
                + f" | {r['date']} | 🎯 {similarity_pct}",
                expanded=(i <= 3)
            ):
                if r.get("author"):
                    st.caption(f"👤 {r['author']}")

                st.markdown(r["text"])

                if r.get("url"):
                    st.markdown(f"[🔗 Відкрити оригінал]({r['url']})")
