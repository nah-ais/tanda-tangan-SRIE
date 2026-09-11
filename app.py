"""
app.py — Laporan Absensi + Tanda Tangan (PDF)
-----------------------------------------------
Aplikasi Streamlit TERPISAH dari sistem SRIE utama. Sekarang mendukung 2 MODE:

  A) FORM LOGIN  -> tabel: No, Nama, Usia, Kelurahan, Tanda Tangan
  B) FORM REGISTER -> tabel: No, Nama, Jenis Kelamin, No. Telepon, Tanda Tangan
     (sebelum PDF dibuat, ada pengecekan data sederhana: hapus duplikat +
     ringkasan EDA singkat, supaya data yang masuk PDF sudah bersih)

ALUR UMUM:
  0. Pilih MODE: "Form Login" atau "Form Register"
  1. Pilih Area Program (dropdown, dari secrets.toml)
  2. Tarik data form terkait
  3. Pilih Tanggal Kegiatan -> Judul Kegiatan (dropdown bertingkat)
  4. (khusus Register) Bersihkan data (drop duplicate) + tampilkan EDA singkat
  5. Klik "Mulai" -> generate PDF
  6. Unduh PDF

CATATAN PENTING SOAL FIELD FORM:
Script ini mencari field berdasarkan NAMA FIELD SAJA (suffix match), bukan
path grup yang persis — supaya tahan terhadap perbedaan struktur grup antar
form/versi.
  - Form Login  punya 2 kemungkinan jalur peserta (dewasa vs anak-via-IDN),
    masing-masing field nama/tanggal-lahir/kelurahan-nya dicoba satu per satu.
  - Form Register punya jalur peserta tunggal (nama_lengkap_parent,
    Jenis_Kelamin, Nomor_WA_HP) sesuai struktur XLSForm register.

CARA JALANKAN:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image as RLImage,
)

DEFAULT_BASE_URL = "https://kf.kobotoolbox.org/api/v2"

# =========================================================
# NAMA FIELD yang dicari (BUKAN path lengkap) — suffix match, lihat
# _find_field_key(). Ganti di sini kalau nama field asli beda.
# =========================================================

# --- Field yang SAMA di kedua form ---
AREA_PROGRAM_FIELD = "Area_Program"
JUDUL_KEGIATAN_FIELD = "Judul_Kegiatan"
TANGGAL_KEGIATAN_FIELD = "Tanggal_Kegiatan"
SIGNATURE_FIELD = "Silahkan_tanda_tangan_disini"

# --- Khusus Form Login ---
# Form Login punya 2 jalur peserta (dewasa vs anak-via-IDN) — tiap field
# dicoba SATU PER SATU (urutan ini), dipakai yang PERTAMA berisi.
LOGIN_NAMA_FIELD_CANDIDATES = ["nama_child", "nama_pulldata_anak"]
LOGIN_TGL_LAHIR_FIELD_CANDIDATES = ["tgl_lahir_child", "tgl_lahir_pulldata_anak"]
LOGIN_KELURAHAN_FIELD_CANDIDATES = ["Kelurahan", "kelurahan_pulldata_anak"]

# --- Khusus Form Register ---
REGISTER_NAMA_FIELD = "nama_lengkap_parent"
REGISTER_JENIS_KELAMIN_FIELD = "Jenis_Kelamin"
REGISTER_PHONE_FIELD = "Nomor_WA_HP"

# Form Register memakai select_one untuk Judul_Kegiatan (list_name: "judul_sementara").
# Kobo menyimpan submission berupa KODE choice (mis. "Learning_gereja_ramah_anak"),
# BUKAN teks labelnya. Mapping ini diambil dari sheet "choices" pada file XLSForm
# Register yang diberikan, supaya dropdown & judul di PDF menampilkan label asli,
# bukan kode mentahnya. Tambahkan baris baru di sini kalau daftar kegiatan bertambah.
REGISTER_JUDUL_KEGIATAN_CHOICES: dict[str, str] = {
    "Learning_gereja_ramah_anak": "ASCA Tiram makmur 1 Kecamatan Cilincing, Marunda, RW 02",
}

# Form Register memakai select_one untuk Area_Program (list_name: "lo6th05"),
# juga tersimpan sebagai KODE. Mapping ini dipakai untuk menampilkan nama AP yang rapi
# kalau dibutuhkan (label ditampilkan, pencocokan data tetap pakai kode aslinya).
REGISTER_AREA_PROGRAM_CHOICES: dict[str, str] = {
    "ADP_Bengkayang": "Project PHINLA",
}


# =========================================================
# SECRETS (Area Program -> Asset UID Form Login / Form Register)
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
    """Baca [kobo.ap.*] dari secrets.toml ->
    {nama_ap: {"login_uid":..., "register_uid":..., "token":..., "base_url":...}}.

    Contoh secrets.toml:
        [kobo]
        default_token = "xxxx"
        default_base_url = "https://kf.kobotoolbox.org/api/v2"

        [kobo.ap.AP_Contoh]
        login_uid = "aXXXXXXXXXXXXXXXXXXXXXX"
        register_uid = "aYYYYYYYYYYYYYYYYYYYYYY"
        # token / base_url opsional, kalau kosong pakai default di atas
    """
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
            "register_uid": values.get("register_uid", ""),
            "token": values.get("token") or default_token,
            "base_url": values.get("base_url") or default_base_url,
        }
    return result


# =========================================================
# UTIL UMUM
# =========================================================
def format_label(text: str) -> str:
    """Ganti underscore dengan spasi supaya enak dibaca (dipakai untuk
    dropdown Judul Kegiatan dan judul di PDF pada Form Login)."""
    return (text or "").replace("_", " ").strip()


def resolve_register_judul_label(kode: str) -> str:
    """Khusus Form Register: ubah KODE Judul_Kegiatan (choice XLSForm) jadi
    label aslinya sesuai sheet 'choices'. Kalau kodenya tidak ada di
    mapping (kegiatan baru yang belum ditambahkan), fallback ke
    underscore->spasi seperti biasa supaya tetap enak dibaca."""
    return REGISTER_JUDUL_KEGIATAN_CHOICES.get(kode, format_label(kode))


def resolve_register_ap_label(kode: str) -> str:
    """Khusus Form Register: ubah KODE Area_Program jadi label aslinya."""
    return REGISTER_AREA_PROGRAM_CHOICES.get(kode, format_label(kode))


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
def fetch_submissions(asset_uid: str, api_token: str, base_url: str) -> list[dict]:
    """Tarik SEMUA submission dari 1 asset Kobo (Login atau Register),
    otomatis mengikuti pagination Kobo."""
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
# PENGECEKAN DATA SEDERHANA (khusus Form Register)
# =========================================================
def clean_and_profile_register_data(rows: list[dict]) -> tuple[list[dict], pd.DataFrame, dict]:
    """
    Lakukan pengecekan data sederhana untuk peserta Form Register:
      1. Hapus data duplikat (berdasarkan kombinasi Nama + Jenis Kelamin + No. Telepon,
         tidak case-sensitive & abaikan spasi berlebih).
      2. EDA sederhana: total data awal, jumlah duplikat, data bersih, data dengan
         field kosong, dan distribusi Jenis Kelamin.

    Return: (rows_bersih, dataframe_bersih, ringkasan_eda)
    """
    df = pd.DataFrame([
        {
            "nama": r.get("nama", "").strip(),
            "jenis_kelamin": r.get("jenis_kelamin", "").strip(),
            "telepon": r.get("telepon", "").strip(),
        }
        for r in rows
    ])

    total_awal = len(df)

    if total_awal == 0:
        return rows, df, {
            "total_awal": 0, "jumlah_duplikat": 0, "total_bersih": 0,
            "data_kosong": 0, "distribusi_gender": {},
        }

    # Kunci pembanding duplikat: dinormalisasi (lowercase + strip spasi)
    key_cols = df.apply(
        lambda row: (
            row["nama"].lower().strip(),
            row["jenis_kelamin"].lower().strip(),
            row["telepon"].strip(),
        ),
        axis=1,
    )
    is_duplicate = key_cols.duplicated(keep="first")
    jumlah_duplikat = int(is_duplicate.sum())

    # Data dengan field penting kosong (nama wajib ada supaya tampil di PDF)
    data_kosong = int((df["nama"] == "").sum())

    # Filter baris asli (rows) sesuai index yang lolos dedup
    keep_mask = ~is_duplicate
    rows_bersih = [r for r, keep in zip(rows, keep_mask) if keep]
    df_bersih = df[keep_mask].reset_index(drop=True)

    distribusi_gender = (
        df_bersih["jenis_kelamin"].replace("", "(kosong)").value_counts().to_dict()
    )

    ringkasan = {
        "total_awal": total_awal,
        "jumlah_duplikat": jumlah_duplikat,
        "total_bersih": len(df_bersih),
        "data_kosong": data_kosong,
        "distribusi_gender": distribusi_gender,
    }
    return rows_bersih, df_bersih, ringkasan


# =========================================================
# PEMBUATAN PDF — KOMPONEN BERSAMA
# =========================================================
def make_signature_flowable(image_bytes: bytes | None, max_width_pt: float = 90, max_height_pt: float = 32):
    """Ubah bytes gambar tanda tangan jadi flowable reportlab, diskalakan proporsional
    supaya muat di dalam sel tabel tanpa distorsi. Kalau gambar tidak ada/rusak,
    kembalikan teks placeholder alih-alih membuat PDF gagal total."""
    placeholder_style = ParagraphStyle(
        "PlaceholderTTD", parent=getSampleStyleSheet()["Normal"],
        fontSize=8, textColor=colors.grey, alignment=TA_CENTER,
    )
    if not image_bytes:
        return Paragraph("(belum ttd)", placeholder_style)
    try:
        pil_img = PILImage.open(io.BytesIO(image_bytes))
        w, h = pil_img.size
        scale = min(max_width_pt / w, max_height_pt / h, 1.0)
        return RLImage(io.BytesIO(image_bytes), width=w * scale, height=h * scale)
    except Exception:
        return Paragraph("(gagal dimuat)", placeholder_style)


def _pdf_styles():
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "JudulKegiatan", parent=styles["Title"],
        fontSize=17, leading=21, alignment=TA_CENTER,
        textColor=colors.HexColor("#111827"), spaceAfter=2,
    )
    subtitle_style = ParagraphStyle(
        "TanggalKegiatan", parent=styles["Normal"],
        fontSize=11, alignment=TA_CENTER,
        textColor=colors.HexColor("#4B5563"), spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "Meta", parent=styles["Normal"],
        fontSize=9, alignment=TA_CENTER,
        textColor=colors.HexColor("#9CA3AF"), spaceAfter=14,
    )
    cell_style = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, leading=11)
    cell_center_style = ParagraphStyle("CellCenter", parent=cell_style, alignment=TA_CENTER)
    footer_style = ParagraphStyle(
        "Footer", parent=styles["Normal"], fontSize=9,
        textColor=colors.HexColor("#374151"),
    )
    return styles, title_style, subtitle_style, meta_style, cell_style, cell_center_style, footer_style


def _table_base_style(header_color: str, n_cols: int) -> TableStyle:
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header_color)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),  # kolom No selalu center
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor(header_color)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F9FAFB")]),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ])


def _build_pdf_header(judul_kegiatan: str, tanggal_kegiatan: str, subjudul: str,
                       title_style, subtitle_style, meta_style) -> list:
    """Header PDF: Judul kegiatan (underscore->spasi) rata tengah, tepat di
    bawahnya sub judul (nama laporan) juga rata tengah, lalu tanggal kegiatan."""
    judul_rapi = format_label(judul_kegiatan) or "(Tanpa Judul Kegiatan)"
    return [
        Paragraph(judul_rapi, title_style),
        Paragraph(subjudul, subtitle_style),
        Paragraph(f"Tanggal Kegiatan: {tanggal_kegiatan or '-'}", meta_style),
    ]


def _build_pdf_footer(total: int, footer_style) -> list:
    generated_at = datetime.now().strftime("%d %B %Y, %H:%M")
    return [
        Spacer(1, 10),
        Table(
            [[Paragraph(f"<b>Total Peserta:</b> {total} orang", footer_style),
              Paragraph(f"Dibuat otomatis: {generated_at}", ParagraphStyle(
                  "FooterRight", parent=footer_style, alignment=2, textColor=colors.HexColor("#9CA3AF"), fontSize=8,
              ))]],
            colWidths=[None, None],
            style=TableStyle([
                ("LINEABOVE", (0, 0), (-1, 0), 0.7, colors.HexColor("#D1D5DB")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]),
        ),
    ]


# =========================================================
# PEMBUATAN PDF — FORM LOGIN
# (Nama, Usia, Kelurahan, Tanda Tangan)
# =========================================================
def build_login_pdf(judul_kegiatan: str, tanggal_kegiatan: str, rows: list[dict], api_token: str) -> bytes:
    """
    rows : list[dict], tiap dict berisi "nama", "usia", "kelurahan", "submission".
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    _, title_style, subtitle_style, meta_style, cell_style, cell_center_style, footer_style = _pdf_styles()

    story = _build_pdf_header(
        judul_kegiatan, tanggal_kegiatan, "Daftar Hadir Peserta (Form Login)",
        title_style, subtitle_style, meta_style,
    )

    table_header = ["No", "Nama", "Usia", "Kelurahan", "Tanda Tangan"]
    table_data = [table_header]

    for i, row in enumerate(rows, start=1):
        signature_bytes = download_signature_bytes(row["submission"], api_token)
        table_data.append([
            str(i),
            Paragraph(row.get("nama", "") or "-", cell_style),
            Paragraph(row.get("usia", "") or "-", cell_center_style),
            Paragraph(row.get("kelurahan", "") or "-", cell_style),
            make_signature_flowable(signature_bytes),
        ])

    table = Table(table_data, colWidths=[1.2 * cm, 5 * cm, 1.8 * cm, 4 * cm, 4 * cm], repeatRows=1)
    table.setStyle(_table_base_style("#1E3A8A", 5))
    story.append(table)
    story.extend(_build_pdf_footer(len(rows), footer_style))

    doc.build(story)
    return buffer.getvalue()


# =========================================================
# PEMBUATAN PDF — FORM REGISTER
# (Nomor otomatis, Nama, Jenis Kelamin, No. Telepon, Tanda Tangan)
# =========================================================
def build_register_pdf(judul_kegiatan: str, tanggal_kegiatan: str, rows: list[dict], api_token: str) -> bytes:
    """
    rows : list[dict], tiap dict berisi "nama", "jenis_kelamin", "telepon", "submission".
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )
    _, title_style, subtitle_style, meta_style, cell_style, cell_center_style, footer_style = _pdf_styles()

    judul_label = resolve_register_judul_label(judul_kegiatan)
    story = _build_pdf_header(
        judul_label, tanggal_kegiatan, "Daftar Hadir Peserta (Form Register)",
        title_style, subtitle_style, meta_style,
    )

    table_header = ["No", "Nama", "Jenis Kelamin", "No. Telepon", "Tanda Tangan"]
    table_data = [table_header]

    for i, row in enumerate(rows, start=1):
        signature_bytes = download_signature_bytes(row["submission"], api_token)
        table_data.append([
            str(i),
            Paragraph(row.get("nama", "") or "-", cell_style),
            Paragraph(row.get("jenis_kelamin", "") or "-", cell_center_style),
            Paragraph(row.get("telepon", "") or "-", cell_center_style),
            make_signature_flowable(signature_bytes),
        ])

    table = Table(table_data, colWidths=[1.2 * cm, 5.3 * cm, 3 * cm, 3.2 * cm, 3.5 * cm], repeatRows=1)
    table.setStyle(_table_base_style("#065F46", 5))
    story.append(table)
    story.extend(_build_pdf_footer(len(rows), footer_style))

    doc.build(story)
    return buffer.getvalue()


# =========================================================
# UI — ALUR FORM LOGIN (kode lama, di-wrap jadi 1 fungsi)
# =========================================================
def run_login_flow(ap_config: dict, selected_ap: str) -> None:
    if not ap_config["login_uid"]:
        st.error(f"⚠️ Asset UID Form Login untuk '{selected_ap}' belum diisi di secrets.toml.")
        st.stop()
        return

    if st.button("🔄 Tarik Data Form Login", use_container_width=True):
        with st.spinner("Mengambil data dari KoboToolbox..."):
            try:
                submissions = fetch_submissions(ap_config["login_uid"], ap_config["token"], ap_config["base_url"])
                st.session_state["submissions_login"] = submissions
                st.session_state["loaded_ap_login"] = selected_ap
                st.success(f"✅ {len(submissions)} data Login berhasil ditarik untuk {selected_ap}.")
            except Exception as e:
                st.error(f"❌ Gagal mengambil data: {e}")

    if "submissions_login" not in st.session_state or st.session_state.get("loaded_ap_login") != selected_ap:
        st.info("📭 Klik tombol di atas untuk menarik data dulu.")
        st.stop()
        return

    submissions = st.session_state["submissions_login"]

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

    selected_judul = st.selectbox("3️⃣ Pilih Judul Kegiatan", judul_list, format_func=format_label)

    matching_submissions = [
        s for s in submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD) == selected_judul
    ]
    st.caption(f"📊 {len(matching_submissions)} peserta ditemukan untuk kegiatan ini.")

    if st.button("🚀 Mulai", type="primary", use_container_width=True):
        if not matching_submissions:
            st.warning("⚠️ Tidak ada peserta untuk kombinasi Tanggal + Judul ini.")
            st.stop()
            return

        with st.spinner("Membuat PDF (termasuk mengunduh gambar tanda tangan)..."):
            rows = []
            for s in matching_submissions:
                nama = _get_first_nonempty(s, LOGIN_NAMA_FIELD_CANDIDATES)
                tgl_lahir = _get_first_nonempty(s, LOGIN_TGL_LAHIR_FIELD_CANDIDATES)
                kelurahan = _get_first_nonempty(s, LOGIN_KELURAHAN_FIELD_CANDIDATES)
                usia = calculate_age(tgl_lahir, selected_tanggal)
                rows.append({"nama": nama, "usia": usia, "kelurahan": kelurahan, "submission": s})

            pdf_bytes = build_login_pdf(selected_judul, selected_tanggal, rows, ap_config["token"])

        st.success("✅ PDF berhasil dibuat.")
        nama_file = f"absensi_login_{format_label(selected_judul)}_{selected_tanggal}.pdf".replace(" ", "_")
        st.download_button(
            "⬇️ Unduh PDF Laporan Absensi (Login)",
            data=pdf_bytes,
            file_name=nama_file,
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )


# =========================================================
# UI — ALUR FORM REGISTER (baru)
# =========================================================
def run_register_flow(ap_config: dict, selected_ap: str) -> None:
    if not ap_config["register_uid"]:
        st.error(f"⚠️ Asset UID Form Register untuk '{selected_ap}' belum diisi di secrets.toml.")
        st.stop()
        return

    if st.button("🔄 Tarik Data Form Register", use_container_width=True):
        with st.spinner("Mengambil data dari KoboToolbox..."):
            try:
                submissions = fetch_submissions(ap_config["register_uid"], ap_config["token"], ap_config["base_url"])
                st.session_state["submissions_register"] = submissions
                st.session_state["loaded_ap_register"] = selected_ap
                st.success(f"✅ {len(submissions)} data Register berhasil ditarik untuk {selected_ap}.")
            except Exception as e:
                st.error(f"❌ Gagal mengambil data: {e}")

    if "submissions_register" not in st.session_state or st.session_state.get("loaded_ap_register") != selected_ap:
        st.info("📭 Klik tombol di atas untuk menarik data dulu.")
        st.stop()
        return

    submissions = st.session_state["submissions_register"]

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

    selected_judul = st.selectbox(
        "3️⃣ Pilih Judul Kegiatan", judul_list, format_func=resolve_register_judul_label,
    )

    matching_submissions = [
        s for s in submissions
        if _get_value(s, TANGGAL_KEGIATAN_FIELD) == selected_tanggal and _get_value(s, JUDUL_KEGIATAN_FIELD) == selected_judul
    ]
    st.caption(f"📊 {len(matching_submissions)} data mentah ditemukan untuk kegiatan ini.")

    if not matching_submissions:
        return

    # --- 4) Pengecekan data sederhana: dedup + EDA singkat ---
    raw_rows = []
    for s in matching_submissions:
        nama = _get_value(s, REGISTER_NAMA_FIELD)
        jenis_kelamin = _get_value(s, REGISTER_JENIS_KELAMIN_FIELD)
        telepon = _get_value(s, REGISTER_PHONE_FIELD)
        raw_rows.append({"nama": nama, "jenis_kelamin": jenis_kelamin, "telepon": telepon, "submission": s})

    rows_bersih, df_bersih, ringkasan = clean_and_profile_register_data(raw_rows)

    st.markdown("#### 🧹 Pengecekan Data")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Data Awal", ringkasan["total_awal"])
    c2.metric("Duplikat Dihapus", ringkasan["jumlah_duplikat"])
    c3.metric("Data Bersih", ringkasan["total_bersih"])
    c4.metric("Nama Kosong", ringkasan["data_kosong"])

    with st.expander("📈 Lihat ringkasan EDA sederhana"):
        if ringkasan["distribusi_gender"]:
            st.write("**Distribusi Jenis Kelamin (setelah dibersihkan):**")
            gender_df = pd.DataFrame(
                list(ringkasan["distribusi_gender"].items()), columns=["Jenis Kelamin", "Jumlah"]
            )
            st.dataframe(gender_df, use_container_width=True, hide_index=True)
        else:
            st.write("Tidak ada data untuk ditampilkan.")

        if not df_bersih.empty:
            st.write("**Contoh data bersih (5 baris pertama):**")
            st.dataframe(df_bersih.head(5), use_container_width=True, hide_index=True)

    if ringkasan["data_kosong"] > 0:
        st.warning(f"⚠️ Ada {ringkasan['data_kosong']} data dengan nama kosong — tetap akan ditampilkan di PDF sebagai '-'.")

    st.caption(f"✅ {len(rows_bersih)} peserta siap dimasukkan ke PDF setelah pembersihan data.")

    # --- 5) Klik Mulai -> generate PDF ---
    if st.button("🚀 Mulai", type="primary", use_container_width=True, key="mulai_register"):
        if not rows_bersih:
            st.warning("⚠️ Tidak ada peserta valid untuk kombinasi Tanggal + Judul ini.")
            st.stop()
            return

        with st.spinner("Membuat PDF (termasuk mengunduh gambar tanda tangan)..."):
            pdf_bytes = build_register_pdf(selected_judul, selected_tanggal, rows_bersih, ap_config["token"])

        st.success("✅ PDF berhasil dibuat.")
        nama_file = f"absensi_register_{resolve_register_judul_label(selected_judul)}_{selected_tanggal}.pdf".replace(" ", "_")
        st.download_button(
            "⬇️ Unduh PDF Laporan Absensi (Register)",
            data=pdf_bytes,
            file_name=nama_file,
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )


# =========================================================
# STREAMLIT UI — ENTRY POINT
# =========================================================
def main() -> None:
    st.set_page_config(page_title="Laporan Absensi + Tanda Tangan", page_icon="📋", layout="centered")
    st.title("📋 Laporan Absensi + Tanda Tangan")
    st.caption("Tarik data dari KoboToolbox, pilih 1 kegiatan, hasilkan PDF absensi bertanda tangan.")

    ap_options = get_ap_options()
    if not ap_options:
        st.error(
            "⚠️ Belum ada Area Program terdaftar di secrets.toml. "
            "Tambahkan blok `[kobo.ap.NamaAP]` (lihat secrets.toml.example)."
        )
        st.stop()
        return

    # --- 0) Pilih Mode Form ---
    mode = st.selectbox("0️⃣ Pilih Jenis Form", ["Form Login", "Form Register"])

    # --- 1) Pilih Area Program ---
    selected_ap = st.selectbox("1️⃣ Pilih Area Program", sorted(ap_options.keys()))
    ap_config = ap_options[selected_ap]

    st.divider()

    if mode == "Form Login":
        run_login_flow(ap_config, selected_ap)
    else:
        run_register_flow(ap_config, selected_ap)


if __name__ == "__main__":
    main()
