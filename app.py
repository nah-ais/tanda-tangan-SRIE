"""
app.py — Laporan Absensi + Tanda Tangan (PDF)
-----------------------------------------------
Aplikasi Streamlit TERPISAH dari sistem SRIE utama. Fungsinya sederhana:
tarik data Form LOGIN dari KoboToolbox, pilih 1 kegiatan spesifik (Tanggal +
Judul), lalu hasilkan laporan PDF berisi tabel peserta (Nama, Usia,
Kelurahan, Tanda Tangan) untuk kegiatan itu.

ALUR:
  1. Pilih Area Program (dropdown, dari secrets.toml)
  2. Tarik data Form Login untuk AP itu
  3. Pilih Tanggal Kegiatan -> Judul Kegiatan (dropdown bertingkat)
  4. Klik "Mulai" -> generate PDF
  5. Unduh PDF

CATATAN PENTING SOAL FIELD FORM:
Sama seperti download_signature_images.py, script ini mencari field
berdasarkan NAMA FIELD SAJA (suffix match), bukan path grup yang persis —
supaya tahan terhadap perbedaan struktur grup antar form/versi. Form Login
saat ini punya 2 kemungkinan jalur peserta (dewasa vs anak-via-IDN),
masing-masing field nama/tanggal-lahir/kelurahan-nya dicoba satu per satu.

CARA JALANKAN:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import io
from datetime import datetime

import requests
import streamlit as st
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage

DEFAULT_BASE_URL = "https://kf.kobotoolbox.org/api/v2"

# =========================================================
# NAMA FIELD yang dicari (BUKAN path lengkap) — suffix match, lihat
# _find_field_key(). Ganti di sini kalau nama field asli beda.
# =========================================================
AREA_PROGRAM_FIELD = "Area_Program"
JUDUL_KEGIATAN_FIELD = "Judul_Kegiatan"
TANGGAL_KEGIATAN_FIELD = "Tanggal_Kegiatan"
SIGNATURE_FIELD = "Silahkan_tanda_tangan_disini"

# Form Login punya 2 jalur peserta (dewasa vs anak-via-IDN) — tiap field
# dicoba SATU PER SATU (urutan ini), dipakai yang PERTAMA berisi.
NAMA_FIELD_CANDIDATES = ["nama_child", "nama_pulldata_anak"]
TGL_LAHIR_FIELD_CANDIDATES = ["tgl_lahir_child", "tgl_lahir_pulldata_anak"]
KELURAHAN_FIELD_CANDIDATES = ["Kelurahan", "kelurahan_pulldata_anak"]


# =========================================================
# SECRETS (Area Program -> Asset UID Form Login)
# =========================================================
def _get_secret(path: list, default=None):
    try:
        node = st.secrets
        for key in path:
            node = node[key]
        return node
    except Exception:
        return default


def get_ap_options() -> dict:
    """Baca [kobo.ap.*] dari secrets.toml -> {nama_ap: {"login_uid":..., "token":..., "base_url":...}}."""
    ap_section = _get_secret(["kobo", "ap"], {})
    default_token = _get_secret(["kobo", "default_token"], "")
    default_base_url = _get_secret(["kobo", "default_base_url"], DEFAULT_BASE_URL)

    result = {}
    try:
        items = ap_section.items()
    except AttributeError:
        items = []
    for ap_name, values in items:
        result[ap_name] = {
            "login_uid": values.get("login_uid", ""),
            "token": values.get("token") or default_token,
            "base_url": values.get("base_url") or default_base_url,
        }
    return result


# =========================================================
# PENGAMBILAN DATA KOBO (suffix-match field, robust terhadap grup)
# =========================================================
def _find_field_key(submission: dict, field_name: str) -> str | None:
    """Cari key yang PERSIS SAMA atau diakhiri '/{field_name}' — tahan nesting grup apa pun."""
    for key in submission.keys():
        if key == field_name or key.endswith("/" + field_name):
            return key
    return None


def _get_value(submission: dict, field_name: str) -> str:
    key = _find_field_key(submission, field_name)
    return str(submission.get(key, "")).strip() if key else ""


def _get_first_nonempty(submission: dict, candidates: list[str]) -> str:
    for field_name in candidates:
        value = _get_value(submission, field_name)
        if value:
            return value
    return ""


@st.cache_data(show_spinner=False, ttl=300)
def fetch_login_submissions(asset_uid: str, api_token: str, base_url: str) -> list[dict]:
    """Tarik SEMUA submission Form Login, otomatis mengikuti pagination Kobo."""
    headers = {"Authorization": f"Token {api_token}"}
    url = f"{base_url.rstrip('/')}/assets/{asset_uid}/data.json"
    params = {"limit": 3000}
    results: list[dict] = []

    while url:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Status HTTP {response.status_code}: {response.text[:300]}")
        payload = response.json()
        results.extend(payload.get("results", []))
        url = payload.get("next")
        params = None

    return results


def calculate_age(tgl_lahir: str, tanggal_acara: str) -> str:
    """Hitung usia PADA SAAT kegiatan berlangsung (bukan usia hari ini)."""
    try:
        born = datetime.strptime(tgl_lahir[:10], "%Y-%m-%d")
        event_date = datetime.strptime(tanggal_acara[:10], "%Y-%m-%d")
        age = event_date.year - born.year - ((event_date.month, event_date.day) < (born.month, born.day))
        return str(age) if age >= 0 else ""
    except (ValueError, TypeError):
        return ""


def download_signature_bytes(submission: dict, api_token: str) -> bytes | None:
    """Unduh gambar tanda tangan submission ini (kalau ada), kembalikan bytes gambarnya."""
    attachments = submission.get("_attachments", [])
    signature_attachment = next(
        (att for att in attachments if str(att.get("question_xpath", "")).endswith(SIGNATURE_FIELD)),
        None,
    )
    if not signature_attachment:
        return None

    download_url = signature_attachment.get("download_url")
    try:
        response = requests.get(download_url, headers={"Authorization": f"Token {api_token}"}, timeout=30)
        if response.status_code == 200:
            return response.content
    except requests.exceptions.RequestException:
        pass
    return None


# =========================================================
# PEMBUATAN PDF
# =========================================================
def make_signature_flowable(image_bytes: bytes | None, max_width_pt: float = 90, max_height_pt: float = 32):
    """Ubah bytes gambar tanda tangan jadi flowable reportlab, diskalakan proporsional
    supaya muat di dalam sel tabel tanpa distorsi. Kalau gambar tidak ada/rusak,
    kembalikan teks placeholder alih-alih membuat PDF gagal total."""
    if not image_bytes:
        return Paragraph("(tidak ada)", getSampleStyleSheet()["Normal"])
    try:
        pil_img = PILImage.open(io.BytesIO(image_bytes))
        w, h = pil_img.size
        scale = min(max_width_pt / w, max_height_pt / h, 1.0)
        return RLImage(io.BytesIO(image_bytes), width=w * scale, height=h * scale)
    except Exception:
        return Paragraph("(gagal dimuat)", getSampleStyleSheet()["Normal"])


def build_attendance_pdf(judul_kegiatan: str, tanggal_kegiatan: str, rows: list[dict], api_token: str) -> bytes:
    """
    Bangun PDF laporan absensi.

    Parameters
    ----------
    rows : list[dict]
        Tiap dict = 1 peserta, minimal berisi key "nama", "usia", "kelurahan",
        dan "submission" (dict submission mentah, untuk cari tanda tangannya).
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("JudulKegiatan", parent=styles["Title"], fontSize=16, spaceAfter=4)
    subtitle_style = ParagraphStyle("TanggalKegiatan", parent=styles["Normal"], fontSize=11, textColor=colors.grey, spaceAfter=16)

    story = [
        Paragraph(judul_kegiatan or "(Tanpa Judul Kegiatan)", title_style),
        Paragraph(f"Tanggal Kegiatan: {tanggal_kegiatan or '-'}", subtitle_style),
    ]

    table_header = ["No", "Nama", "Usia", "Kelurahan", "Tanda Tangan"]
    table_data = [table_header]
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9)

    for i, row in enumerate(rows, start=1):
        signature_bytes = download_signature_bytes(row["submission"], api_token)
        table_data.append([
            str(i),
            Paragraph(row.get("nama", "") or "-", cell_style),
            row.get("usia", "") or "-",
            Paragraph(row.get("kelurahan", "") or "-", cell_style),
            make_signature_flowable(signature_bytes),
        ])

    table = Table(table_data, colWidths=[1.2 * cm, 5 * cm, 1.8 * cm, 4 * cm, 4 * cm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F4F6")]),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 12))
    story.append(Paragraph(f"Total Peserta: {len(rows)}", styles["Normal"]))

    doc.build(story)
    return buffer.getvalue()


# =========================================================
# STREAMLIT UI
# =========================================================
def main() -> None:
    st.set_page_config(page_title="Laporan Absensi + Tanda Tangan", page_icon="📋", layout="centered")
    st.title("📋 Laporan Absensi + Tanda Tangan")
    st.caption("Tarik data Form Login, pilih 1 kegiatan, hasilkan PDF absensi bertanda tangan.")

    ap_options = get_ap_options()
    if not ap_options:
        st.error(
            "⚠️ Belum ada Area Program terdaftar di secrets.toml. "
            "Tambahkan blok `[kobo.ap.NamaAP]` (lihat secrets.toml.example)."
        )
        st.stop()
        return

    # --- 1) Pilih Area Program ---
    selected_ap = st.selectbox("1️⃣ Pilih Area Program", sorted(ap_options.keys()))
    ap_config = ap_options[selected_ap]

    if not ap_config["login_uid"]:
        st.error(f"⚠️ Asset UID Form Login untuk '{selected_ap}' belum diisi di secrets.toml.")
        st.stop()
        return

    # --- 2) Tarik data Form Login ---
    if st.button("🔄 Tarik Data Form Login", use_container_width=True):
        with st.spinner("Mengambil data dari KoboToolbox..."):
            try:
                submissions = fetch_login_submissions(ap_config["login_uid"], ap_config["token"], ap_config["base_url"])
                st.session_state["submissions"] = submissions
                st.session_state["loaded_ap"] = selected_ap
                st.success(f"✅ {len(submissions)} data Login berhasil ditarik untuk {selected_ap}.")
            except Exception as e:
                st.error(f"❌ Gagal mengambil data: {e}")

    if "submissions" not in st.session_state or st.session_state.get("loaded_ap") != selected_ap:
        st.info("📭 Klik tombol di atas untuk menarik data dulu.")
        st.stop()
        return

    submissions = st.session_state["submissions"]

    # --- 3) Pilih Tanggal Kegiatan -> Judul Kegiatan (bertingkat) ---
    tanggal_list = sorted({_get_value(s, TANGGAL_KEGIATAN_FIELD) for s in submissions if _get_value(s, TANGGAL_KEGIATAN_FIELD)})
    if not tanggal_list:
        st.warning("⚠️ Tidak ada data dengan Tanggal Kegiatan yang terisi.")
        st.stop()
        return

    selected_tanggal = st.selectbox("2️⃣ Pilih Tanggal Kegiatan", tanggal_list)

    judul_list = sorted({
        _get_value(s, JUDUL_KEGIATAN_FIELD) for s in submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD)
    })
    if not judul_list:
        st.warning(f"⚠️ Tidak ada Judul Kegiatan untuk tanggal {selected_tanggal}.")
        st.stop()
        return

    selected_judul = st.selectbox("3️⃣ Pilih Judul Kegiatan", judul_list)

    matching_submissions = [
        s for s in submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD) == selected_judul
    ]
    st.caption(f"📊 {len(matching_submissions)} peserta ditemukan untuk kegiatan ini.")

    # --- 4) Klik Mulai -> generate PDF ---
    if st.button("🚀 Mulai", type="primary", use_container_width=True):
        if not matching_submissions:
            st.warning("⚠️ Tidak ada peserta untuk kombinasi Tanggal + Judul ini.")
            st.stop()
            return

        with st.spinner("Membuat PDF (termasuk mengunduh gambar tanda tangan)..."):
            rows = []
            for s in matching_submissions:
                nama = _get_first_nonempty(s, NAMA_FIELD_CANDIDATES)
                tgl_lahir = _get_first_nonempty(s, TGL_LAHIR_FIELD_CANDIDATES)
                kelurahan = _get_first_nonempty(s, KELURAHAN_FIELD_CANDIDATES)
                usia = calculate_age(tgl_lahir, selected_tanggal)
                rows.append({"nama": nama, "usia": usia, "kelurahan": kelurahan, "submission": s})

            pdf_bytes = build_attendance_pdf(selected_judul, selected_tanggal, rows, ap_config["token"])

        st.success("✅ PDF berhasil dibuat.")
        st.download_button(
            "⬇️ Unduh PDF Laporan Absensi",
            data=pdf_bytes,
            file_name=f"absensi_{selected_judul}_{selected_tanggal}.pdf".replace(" ", "_"),
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )


if __name__ == "__main__":
    main()
