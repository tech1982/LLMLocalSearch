"""
Streamlit web UI for semantic search across Telegram & Instagram.
"""
import streamlit as st
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from search_engine import search, generate_answer, get_stats

st.set_page_config(
    page_title="🔍 TG & Insta Search",
    page_icon="🔍",
    layout="wide"
)

st.title("🔍 Semantic Search")
st.caption("Telegram · Instagram — search by meaning and context, not just keywords")

# Sidebar with stats and settings
with st.sidebar:
    st.header("⚙️ Settings")

    source = st.selectbox(
        "Source",
        ["all", "telegram", "instagram"],
        format_func=lambda x: {"all": "🌐 All sources", "telegram": "📱 Telegram", "instagram": "📸 Instagram"}[x]
    )

    n_results = st.slider("Number of results", 3, 20, 7)

    use_llm = st.toggle("🤖 Generate answer (Ollama)", value=True)

    answer_lang = st.selectbox(
        "Answer language",
        ["en", "uk", "ru", "pl"],
        format_func=lambda x: {"en": "🇬🇧 English", "uk": "🇺🇦 Ukrainian", "ru": "🇷🇺 Russian", "pl": "🇵🇱 Polish"}[x]
    )

    st.divider()
    st.header("📊 Statistics")
    stats = get_stats()
    col1, col2 = st.columns(2)
    col1.metric("📱 Telegram", stats["telegram"])
    col2.metric("📸 Instagram", stats["instagram"])
    st.metric("📄 Total documents", stats["total"])

    st.divider()
    st.caption("To index new data:")
    st.code("docker exec -it semantic-search \\\n  python src/ingest_telegram.py", language="bash")
    st.code("docker exec -it semantic-search \\\n  python src/ingest_instagram.py", language="bash")

# Main search interface
query = st.text_input(
    "Ask a question",
    placeholder="For example: What housing rental advice is available in Warsaw?",
    label_visibility="collapsed"
)

if query:
    with st.spinner("🔎 Searching..."):
        results = search(
            query=query,
            n_results=n_results,
            source_filter=source if source != "all" else None
        )

    if not results:
        st.warning("No results found. Try rephrasing your query or make sure indexing has been run.")
    else:
        # LLM Answer
        if use_llm:
            with st.spinner("🤖 Generating answer..."):
                answer = generate_answer(query, results, language=answer_lang)
            st.markdown("### 💡 Answer")
            st.markdown(answer)
            st.divider()

        # Raw results
        st.markdown(f"### 📄 Found {len(results)} relevant messages")

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
                    st.markdown(f"[🔗 Open original]({r['url']})")
