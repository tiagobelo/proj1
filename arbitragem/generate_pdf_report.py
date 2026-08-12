"""
Gera um PDF apresentável, para enviar a sellers/clientes interessados em
revender, com só os produtos classificados como 🟢 "excelente
oportunidade" por generate_report.py.

Usa o mesmo CSV de entrada (gerado por compare_mercadolivre.py) e os
mesmos parâmetros de custo (comissão, custo fixo, frete, imposto) que
generate_report.py, e reaproveita compute_opportunities() de lá — o PDF
e o XLSX interno nunca divergem na lógica de cálculo.

O PDF traz, logo no topo, um aviso sobre custo fixo e frete serem
estimativas (não os valores exatos que o Mercado Livre vai cobrar) — o
mesmo aviso é repetido, resumido, no rodapé de cada página, já que este
documento pode ser encaminhado adiante e nem sempre é lido do início.

Dependência: reportlab, PyYAML (pip install reportlab pyyaml)

Uso:
    python generate_pdf_report.py comparacao.csv
    python generate_pdf_report.py comparacao.csv --output top_oportunidades.pdf --shipping-cost 15
    python generate_pdf_report.py comparacao.csv --min-margin-pct 20 --min-roi-pct 25 \
        --title "Oportunidades de Revenda — Semana 32"
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from generate_report import (
    DEFAULT_FEES_CONFIG_PATH,
    DEFAULT_FIXED_FEE_TIERS_PATH,
    FALLBACK_FIXED_FEE,
    compute_opportunities,
    load_fees_config,
    load_fixed_fee_tiers,
)

PAGE_MARGIN = 2 * cm

INK = colors.HexColor("#111827")
MUTED = colors.HexColor("#6B7280")
BRAND = colors.HexColor("#059669")
BORDER = colors.HexColor("#E5E7EB")
WARNING_BG = colors.HexColor("#FEF3C7")
WARNING_BORDER = colors.HexColor("#D97706")
CARD_BG = colors.HexColor("#F9FAFB")
LINK_COLOR = colors.HexColor("#2563EB")

STYLES = getSampleStyleSheet()
STYLES.add(ParagraphStyle("DocTitle", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK, spaceAfter=8))
STYLES.add(ParagraphStyle("DocSubtitle", fontName="Helvetica", fontSize=10.5, leading=13, textColor=MUTED, spaceAfter=0))
STYLES.add(ParagraphStyle("WarningTitle", fontName="Helvetica-Bold", fontSize=10.5, textColor=colors.HexColor("#92400E")))
STYLES.add(ParagraphStyle("WarningBody", fontName="Helvetica", fontSize=9, leading=13, textColor=colors.HexColor("#78350F")))
STYLES.add(ParagraphStyle("ProductTitle", fontName="Helvetica-Bold", fontSize=12.5, textColor=INK, leading=15))
STYLES.add(ParagraphStyle("ProductMeta", fontName="Helvetica", fontSize=8.5, textColor=MUTED, spaceAfter=0))
STYLES.add(ParagraphStyle("StatLabel", fontName="Helvetica", fontSize=7.5, textColor=MUTED))
STYLES.add(ParagraphStyle("StatValue", fontName="Helvetica-Bold", fontSize=12, textColor=INK, leading=14))
STYLES.add(ParagraphStyle("StatValueGood", fontName="Helvetica-Bold", fontSize=12, textColor=BRAND, leading=14))
STYLES.add(ParagraphStyle("LinkLine", fontName="Helvetica", fontSize=9, textColor=LINK_COLOR))
STYLES.add(ParagraphStyle("EmptyState", fontName="Helvetica", fontSize=11, textColor=MUTED, alignment=TA_CENTER))


def fmt_brl(value: float | None) -> str:
    if value is None:
        return "—"
    formatted = f"{value:,.2f}"  # ex.: "1,234.56"
    integer_part, _, decimal_part = formatted.partition(".")
    integer_part = integer_part.replace(",", ".")  # "1.234"
    return f"R$ {integer_part},{decimal_part}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".replace(".", ",") + "%"


def stat_cell(label: str, value: str, good: bool = False) -> Table:
    style = "StatValueGood" if good else "StatValue"
    t = Table(
        [[Paragraph(escape(label).upper(), STYLES["StatLabel"])], [Paragraph(escape(value), STYLES[style])]],
        colWidths=[None],
    )
    t.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def build_product_card(opp: dict) -> KeepTogether:
    title = Paragraph(escape(opp["title"] or "(sem título)"), STYLES["ProductTitle"])
    meta_bits = [f"ASIN {opp['asin']}"]
    if opp.get("domain_id"):
        meta_bits.append(escape(opp["domain_id"]))
    if opp.get("listing_count") not in (None, ""):
        meta_bits.append(f"{opp['listing_count']} anúncio(s) no ML")
    meta = Paragraph(" · ".join(meta_bits), STYLES["ProductMeta"])

    stats_row1 = Table(
        [[
            stat_cell("Preço Amazon (custo)", fmt_brl(opp["cost"])),
            stat_cell("Venda estimada no ML", fmt_brl(opp["sale_price"])),
            stat_cell("Lucro líquido estimado", fmt_brl(opp["net_profit"]), good=True),
            stat_cell("Margem", fmt_pct(opp["margin_pct"]), good=True),
        ]],
        colWidths=["25%", "25%", "25%", "25%"],
    )
    stats_row1.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    stats_row2 = Table(
        [[
            stat_cell("ROI", fmt_pct(opp["roi_pct"])),
            stat_cell(f"Comissão ML ({opp['commission_pct']:.1f}%)", fmt_brl(opp["commission"])),
            stat_cell("Custo fixo aplicado", fmt_brl(opp["fixed_fee"])),
            stat_cell("Frete aplicado", fmt_brl(opp["shipping"])),
        ]],
        colWidths=["25%", "25%", "25%", "25%"],
    )
    stats_row2.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8)]))

    links = Paragraph(
        f'<a href="{escape(opp["amazon_url"])}" color="#2563EB"><u>Ver na Amazon →</u></a>'
        "&nbsp;&nbsp;&nbsp;&nbsp;"
        f'<a href="{escape(opp["ml_url"])}" color="#2563EB"><u>Ver anúncio no Mercado Livre →</u></a>',
        STYLES["LinkLine"],
    )

    # Uma célula só contendo a lista de flowables empilhados — se cada
    # flowable virasse uma linha própria da Table, o padding de célula
    # (TOPPADDING/BOTTOMPADDING) seria aplicado a CADA linha em vez de uma
    # vez só ao redor do bloco inteiro, inflando o espaço em branco entre
    # os elementos do card.
    card_content = [title, meta, Spacer(1, 6), stats_row1, stats_row2, Spacer(1, 8), links]
    card = Table([[card_content]], colWidths=["100%"])
    card.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return KeepTogether([card, Spacer(1, 14)])


def build_warning_box(shipping_cost: float, fixed_fee_override: float | None, has_fixed_fee_tiers: bool) -> Table:
    if fixed_fee_override is not None:
        fixed_fee_note = f"custo fixo único de {fmt_brl(fixed_fee_override)} aplicado a todos os produtos"
    elif has_fixed_fee_tiers:
        fixed_fee_note = "custo fixo calculado por faixa de preço de venda"
    else:
        fixed_fee_note = f"custo fixo de {fmt_brl(FALLBACK_FIXED_FEE)} aplicado a todos os produtos (valor padrão, faixas ainda não configuradas)"

    body_lines = [
        f"Este relatório usa <b>{escape(fixed_fee_note)}</b> e <b>frete de {fmt_brl(shipping_cost)} por venda</b>. "
        "Desde março/2026 o Mercado Livre calcula esse custo operacional por <b>peso e dimensão</b> do produto, "
        "não mais por uma taxa fixa simples — os valores aqui são <b>estimativas</b>, não os valores exatos que "
        "serão cobrados.",
        "Antes de comprar ou revender qualquer item deste relatório, confirme os custos reais (comissão, custo "
        "fixo e frete) no Simulador de Custos oficial do Mercado Livre.",
    ]
    if shipping_cost == 0.0:
        body_lines.append(
            "<b>Atenção:</b> o frete considerado neste relatório é R$ 0,00. Frete costuma ser um custo real e "
            "relevante — os números de lucro/margem abaixo podem estar otimistas."
        )

    cell_content = [
        Paragraph("LEIA COM ATENÇÃO ANTES DE DECIDIR", STYLES["WarningTitle"]),
        Spacer(1, 4),
    ]
    for line in body_lines:
        cell_content.append(Paragraph(line, STYLES["WarningBody"]))
        cell_content.append(Spacer(1, 4))

    box = Table([[cell_content]], colWidths=["100%"])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), WARNING_BG),
                ("BOX", (0, 0), (-1, -1), 1, WARNING_BORDER),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return box


def make_footer(generated_at: str):
    def _footer(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(BORDER)
        canvas.line(PAGE_MARGIN, 1.6 * cm, doc.pagesize[0] - PAGE_MARGIN, 1.6 * cm)
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(PAGE_MARGIN, 1.2 * cm, f"Gerado em {generated_at}")
        canvas.drawCentredString(
            doc.pagesize[0] / 2,
            1.2 * cm,
            "Custo fixo e frete são estimativas — confirme os valores reais antes de decidir.",
        )
        canvas.drawRightString(doc.pagesize[0] - PAGE_MARGIN, 1.2 * cm, f"Página {doc.page}")
        canvas.restoreState()

    return _footer


def build_pdf(
    excellent: list[dict],
    output: str,
    title: str,
    price_basis: str,
    shipping_cost: float,
    fixed_fee_override: float | None,
    has_fixed_fee_tiers: bool,
) -> None:
    generated_at = datetime.now().strftime("%d/%m/%Y %H:%M")
    basis_label = {"median": "mediana", "avg": "média", "min": "mínimo"}.get(price_basis, price_basis)

    doc = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=2.1 * cm,
        title=title,
    )

    story = [
        Paragraph(escape(title), STYLES["DocTitle"]),
        Paragraph(
            f"{len(excellent)} produto(s) com melhor oportunidade de revenda · "
            f"preço de venda no Mercado Livre pelo {basis_label}",
            STYLES["DocSubtitle"],
        ),
        Spacer(1, 14),
        build_warning_box(shipping_cost, fixed_fee_override, has_fixed_fee_tiers),
        Spacer(1, 18),
    ]

    if not excellent:
        story.append(Spacer(1, 40))
        story.append(
            Paragraph(
                'Nenhum produto foi classificado como <font color="#059669"><b>Excelente Oportunidade</b></font> nesta rodada.',
                STYLES["EmptyState"],
            )
        )
    else:
        for opp in excellent:
            story.append(build_product_card(opp))

    footer = make_footer(generated_at)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gera um PDF apresentável (só produtos 🟢 excelente) para enviar a sellers/clientes"
    )
    parser.add_argument("input_csv", help="CSV gerado por compare_mercadolivre.py")
    parser.add_argument("--output", default="oportunidades.pdf", help="Caminho do PDF de saída")
    parser.add_argument("--title", default="Oportunidades de Revenda — Amazon → Mercado Livre", help="Título exibido no topo do PDF")
    parser.add_argument("--price-basis", choices=["median", "avg", "min"], default="median", help="Qual preço do Mercado Livre usar como preço de venda esperado (padrão: median)")
    parser.add_argument("--commission-pct", type=float, default=None, help="Comissão do Mercado Livre em %% fixa para todas as linhas. Se omitido, escolhe automaticamente por categoria via config/ml_fees.yaml")
    parser.add_argument("--fees-config", default=str(DEFAULT_FEES_CONFIG_PATH), help="Caminho do YAML de comissão por categoria (padrão: config/ml_fees.yaml)")
    parser.add_argument("--fixed-fee", type=float, default=None, help="Custo fixo em R$ fixo para TODAS as linhas. Se omitido, escolhe pela faixa de preço de venda via config/ml_fixed_fee_tiers.yaml (fallback R$ 6,00 se vazio)")
    parser.add_argument("--fixed-fee-config", default=str(DEFAULT_FIXED_FEE_TIERS_PATH), help="Caminho do YAML de custo fixo por faixa de preço (padrão: config/ml_fixed_fee_tiers.yaml)")
    parser.add_argument("--shipping-cost", type=float, default=0.0, help="Frete estimado em R$ que o vendedor absorve (padrão 0)")
    parser.add_argument("--tax-pct", type=float, default=0.0, help="Imposto sobre a venda em %% (padrão 0)")
    parser.add_argument("--min-margin-pct", type=float, default=15.0, help="Margem mínima (%%) para classificar como 'excelente oportunidade' (padrão 15)")
    parser.add_argument("--min-roi-pct", type=float, default=20.0, help="ROI mínimo (%%) para classificar como 'excelente oportunidade' (padrão 20)")
    args = parser.parse_args()

    if args.shipping_cost == 0.0:
        print("Aviso: --shipping-cost está em 0. Frete costuma ser um custo real e relevante — considere ajustar.\n")

    fees_config = load_fees_config(Path(args.fees_config))
    fixed_fee_tiers = load_fixed_fee_tiers(Path(args.fixed_fee_config))
    if args.fixed_fee is None and not fixed_fee_tiers:
        print(
            f"Aviso: {args.fixed_fee_config} não tem faixas configuradas (tiers: []) — "
            f"usando R$ {FALLBACK_FIXED_FEE:.2f} fixo para todas as linhas.\n"
        )

    with open(args.input_csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    opportunities = compute_opportunities(
        rows,
        args.price_basis,
        fees_config,
        fixed_fee_tiers,
        args.commission_pct,
        args.fixed_fee,
        args.shipping_cost,
        args.tax_pct,
        args.min_margin_pct,
        args.min_roi_pct,
    )

    excellent = [o for o in opportunities if o["status_emoji"] == "🟢"]
    excellent.sort(key=lambda o: (o["margin_pct"] is None, -(o["margin_pct"] or 0)))

    build_pdf(
        excellent,
        args.output,
        args.title,
        args.price_basis,
        args.shipping_cost,
        args.fixed_fee,
        bool(fixed_fee_tiers),
    )

    print(f"PDF salvo em {args.output}")
    print(f"  {len(excellent)} de {len(opportunities)} produto(s) classificados como 🟢 excelente oportunidade")


if __name__ == "__main__":
    main()
