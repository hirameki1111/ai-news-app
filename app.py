import streamlit as st
import pandas as pd
import json
import re
from google import genai
from google.genai import types
from supabase import create_client

# ── 페이지 기본 설정 ──────────────────────────────────────────────
st.set_page_config(page_title="AI 뉴스 검색기", page_icon="📰", layout="wide")
st.title("📰 AI 최신 뉴스 검색 & 자동 저장기")

# ── 시크릿 로드 및 클라이언트 초기화 ──────────────────────────────
@st.cache_resource
def init_clients():
    gemini = genai.Client(api_key=st.secrets["GEMINI_API_KEY"])
    supa   = create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])
    return gemini, supa

gemini_client, supabase = init_clients()

# ── 탭 구성 ───────────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["🔍 검색하기", "💾 저장된 뉴스 보기", "📊 통계 분석"])


# ═══════════════════════════════════════════════════════════════════
# Tab 1 · 검색하기
# ═══════════════════════════════════════════════════════════════════
with tab1:
    st.subheader("🔍 키워드로 최신 뉴스 검색")
    keyword = st.text_input("검색할 키워드를 입력하세요", placeholder="예: 인공지능, 트럼프, 반도체")
    search_btn = st.button("🚀 검색 및 저장", use_container_width=True)

    if search_btn and keyword.strip():
        with st.spinner("Gemini가 최신 뉴스를 검색 중입니다..."):

            # ── 1단계: Gemini 호출 (google_search 도구 활성화) ──────
            prompt = f"""
'{keyword}'에 대한 가장 최신 뉴스 딱 2건만 검색해.
반드시 아래 JSON 배열 형식으로만 응답해. 마크다운 코드블록 없이 순수 JSON만 출력해.
절대로 URL을 지어내지 마. 모르면 url 값을 빈 문자열("")로 남겨.

[
  {{
    "title": "기사 제목",
    "source": "언론사명",
    "news_date": "YYYY-MM-DD 형식 날짜",
    "url": "원본 기사 URL",
    "summary": "2~3문장 요약"
  }},
  ...
]
"""
            response = gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )

            # ── 2단계: grounding_chunks에서 실제 URL 추출 ───────────
            real_urls: list[dict] = []
            try:
                chunks = (
                    response.candidates[0]
                    .grounding_metadata
                    .grounding_chunks
                )
                for chunk in chunks:
                    if hasattr(chunk, "web") and chunk.web:
                        uri = chunk.web.uri or ""
                        title = chunk.web.title or ""
                        # 구글 내부 redirect 링크 제외
                        if uri.startswith("http") and "grounding-api-redirect" not in uri:
                            real_urls.append({"title": title, "uri": uri})
            except Exception:
                pass  # grounding_metadata 없으면 그냥 진행

            # ── 3단계: JSON 파싱 ─────────────────────────────────────
            raw_text = response.text.strip()
            # 코드블록 마크다운 제거 (만약 붙어오면)
            raw_text = re.sub(r"```(?:json)?", "", raw_text).strip().rstrip("```").strip()

            news_items: list[dict] = []
            try:
                news_items = json.loads(raw_text)
                if not isinstance(news_items, list):
                    news_items = []
            except json.JSONDecodeError:
                # JSON 배열 부분만 추출 시도
                match = re.search(r"\[.*\]", raw_text, re.DOTALL)
                if match:
                    try:
                        news_items = json.loads(match.group())
                    except Exception:
                        news_items = []

            if not news_items:
                st.error("뉴스를 파싱하지 못했습니다. 키워드를 바꿔 다시 시도해 보세요.")
                st.stop()

            # ── 4단계: URL 환각 방지 — grounding URL로 덮어쓰기 ──────
            for item in news_items:
                item_title_lower = (item.get("title") or "").lower()
                for real in real_urls:
                    real_title_lower = real["title"].lower()
                    # 제목이 50% 이상 겹치면 실제 URL로 교체
                    # 간단 휴리스틱: 공통 단어 비교
                    item_words  = set(item_title_lower.split())
                    real_words  = set(real_title_lower.split())
                    if item_words and real_words:
                        overlap = len(item_words & real_words) / max(len(item_words), len(real_words))
                        if overlap >= 0.4:
                            item["url"] = real["uri"]
                            break
                    # 제목 비교 실패해도 첫 번째 real_url로 채우기 (url이 비어 있을 때)
                if not (item.get("url") or "").startswith("http") and real_urls:
                    item["url"] = real_urls[0]["uri"]

            # ── 5단계: 카드 형태로 화면 출력 ────────────────────────
            st.success(f"✅ {len(news_items)}건의 뉴스를 찾았습니다!")
            for i, item in enumerate(news_items, 1):
                with st.container(border=True):
                    st.markdown(f"### {i}. {item.get('title','(제목 없음)')}")
                    col_a, col_b = st.columns(2)
                    col_a.markdown(f"**📡 출처:** {item.get('source','')}")
                    col_b.markdown(f"**📅 날짜:** {item.get('news_date','')}")
                    st.markdown(f"**📝 요약:** {item.get('summary','')}")
                    url_val = item.get("url","")
                    if url_val.startswith("http"):
                        st.markdown(f"**🔗 링크:** [{url_val}]({url_val})")
                    else:
                        st.markdown("**🔗 링크:** (URL 확인 불가)")

            # ── 6단계: Supabase 저장 (중복 URL은 생략) ───────────────
            saved, skipped = 0, 0
            for item in news_items:
                try:
                    supabase.table("news_history").insert({
                        "keyword":   keyword.strip(),
                        "title":     item.get("title",""),
                        "source":    item.get("source",""),
                        "news_date": item.get("news_date",""),
                        "url":       item.get("url","") or None,
                        "summary":   item.get("summary",""),
                    }).execute()
                    saved += 1
                except Exception as e:
                    # PostgreSQL 중복 키 에러 코드 23505
                    if "23505" in str(e):
                        skipped += 1
                    else:
                        st.warning(f"저장 오류: {e}")

            msg = f"💾 저장 완료: {saved}건 저장"
            if skipped:
                msg += f" / {skipped}건 중복 생략"
            st.toast(msg, icon="✅")

    elif search_btn:
        st.warning("키워드를 입력해 주세요.")


# ═══════════════════════════════════════════════════════════════════
# Tab 2 · 저장된 뉴스 보기
# ═══════════════════════════════════════════════════════════════════
with tab2:
    st.subheader("💾 저장된 뉴스 목록")

    @st.cache_data(ttl=30)
    def load_news():
        resp = (
            supabase.table("news_history")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()

    df = load_news()

    if df.empty:
        st.info("아직 저장된 뉴스가 없습니다. Tab 1에서 검색해 보세요!")
    else:
        # 필터링
        search_filter = st.text_input("🔎 제목 또는 키워드로 필터링", placeholder="검색어 입력...")
        if search_filter:
            mask = (
                df["title"].str.contains(search_filter, case=False, na=False) |
                df["keyword"].str.contains(search_filter, case=False, na=False)
            )
            df_view = df[mask]
        else:
            df_view = df

        st.caption(f"총 {len(df_view)}건 표시 중")

        # 컬럼 순서 정리
        display_cols = ["id","keyword","title","source","news_date","url","summary","created_at"]
        display_cols = [c for c in display_cols if c in df_view.columns]
        st.dataframe(df_view[display_cols], use_container_width=True, hide_index=True)

        # CSV 다운로드
        csv_data = df_view[display_cols].to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="⬇️ 현재 목록 CSV 다운로드",
            data=csv_data,
            file_name="news_history.csv",
            mime="text/csv",
        )

    if st.button("🔄 새로고침"):
        st.cache_data.clear()
        st.rerun()


# ═══════════════════════════════════════════════════════════════════
# Tab 3 · 통계 분석
# ═══════════════════════════════════════════════════════════════════
with tab3:
    st.subheader("📊 저장 통계 대시보드")

    @st.cache_data(ttl=30)
    def load_stats():
        resp = (
            supabase.table("news_history")
            .select("keyword, created_at")
            .execute()
        )
        return pd.DataFrame(resp.data) if resp.data else pd.DataFrame()

    df_stat = load_stats()

    if df_stat.empty:
        st.info("통계를 표시할 데이터가 없습니다.")
    else:
        col_left, col_right = st.columns(2)

        # ── 왼쪽: 키워드별 누적 건수 ─────────────────────────────
        with col_left:
            st.markdown("#### 🏷️ 키워드별 누적 검색 건수")
            keyword_counts = (
                df_stat["keyword"]
                .value_counts()
                .rename_axis("keyword")
                .reset_index(name="건수")
                .set_index("keyword")
            )
            st.bar_chart(keyword_counts)

        # ── 오른쪽: 일자별 저장 건수 ─────────────────────────────
        with col_right:
            st.markdown("#### 📅 일자별 저장 건수")
            df_stat["date"] = pd.to_datetime(df_stat["created_at"]).dt.strftime("%Y-%m-%d")
            date_counts = (
                df_stat["date"]
                .value_counts()
                .sort_index()
                .rename_axis("날짜")
                .reset_index(name="건수")
                .set_index("날짜")
            )
            st.line_chart(date_counts)

    if st.button("🔄 통계 새로고침"):
        st.cache_data.clear()
        st.rerun()