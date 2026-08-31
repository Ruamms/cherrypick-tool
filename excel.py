"""Escreve .xlsx sem dependencia externa.

Um xlsx e um zip de XML. Escrever na mao custa ~100 linhas e mantem a promessa do
projeto: o exe embute so o Python, nada de biblioteca de terceiros para pesar o
download e o build.

Suporta o que a exportacao precisa e nada mais: varias planilhas, cabecalho em
negrito congelado, filtro automatico e celula numerica de verdade (para o Excel
ordenar DIAS como numero, nao como texto).
"""

import re
import zipfile

_CONTROLES = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

TIPOS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
%s</Types>"""

RELS_RAIZ = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

ESTILOS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def coluna(indice):
    """0 -> A, 25 -> Z, 26 -> AA."""
    nome = ""
    indice += 1
    while indice:
        indice, resto = divmod(indice - 1, 26)
        nome = chr(65 + resto) + nome
    return nome


def _texto(valor):
    texto = "" if valor is None else str(valor)
    texto = _CONTROLES.sub("", texto)
    return (texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _celula(ref, valor, negrito=False):
    estilo = ' s="1"' if negrito else ""
    if isinstance(valor, bool):
        valor = "sim" if valor else "nao"
    if isinstance(valor, (int, float)):
        return '<c r="%s"%s><v>%s</v></c>' % (ref, estilo, valor)
    texto = _texto(valor)
    if not texto:
        return '<c r="%s"%s/>' % (ref, estilo)
    return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, estilo, texto)


def _planilha(cabecalho, linhas, larguras=None):
    total_colunas = max([len(cabecalho)] + [len(l) for l in linhas]) if cabecalho else 1
    partes = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
              '<dimension ref="A1:%s%d"/>' % (coluna(total_colunas - 1), len(linhas) + 1),
              '<sheetViews><sheetView workbookViewId="0">'
              '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
              '</sheetView></sheetViews>']
    if larguras:
        partes.append("<cols>")
        for i, largura in enumerate(larguras[:total_colunas]):
            partes.append('<col min="%d" max="%d" width="%d" customWidth="1"/>'
                          % (i + 1, i + 1, largura))
        partes.append("</cols>")
    partes.append("<sheetData>")
    partes.append('<row r="1">%s</row>' % "".join(
        _celula("%s1" % coluna(i), v, negrito=True) for i, v in enumerate(cabecalho)))
    for numero, linha in enumerate(linhas, 2):
        partes.append('<row r="%d">%s</row>' % (numero, "".join(
            _celula("%s%d" % (coluna(i), numero), v) for i, v in enumerate(linha))))
    partes.append("</sheetData>")
    partes.append('<autoFilter ref="A1:%s%d"/>' % (coluna(total_colunas - 1), len(linhas) + 1))
    partes.append("</worksheet>")
    return "".join(partes)


def escrever(caminho, planilhas):
    """planilhas: [(nome, cabecalho, linhas, larguras)]. Sobrescreve o arquivo."""
    planilhas = [p for p in planilhas if p]
    if not planilhas:
        raise ValueError("Nada para exportar.")
    overrides = "".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n' % (i + 1)
        for i in range(len(planilhas)))
    abas = "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                   % (_texto(nome)[:31], i + 1, i + 1)
                   for i, (nome, _c, _l, _w) in enumerate(planilhas))
    rels = "".join(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
        for i in range(len(planilhas)))
    rels += ('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
             'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
             % (len(planilhas) + 1))

    with zipfile.ZipFile(caminho, "w", zipfile.ZIP_DEFLATED) as zip_saida:
        zip_saida.writestr("[Content_Types].xml", TIPOS % overrides)
        zip_saida.writestr("_rels/.rels", RELS_RAIZ)
        zip_saida.writestr("xl/workbook.xml",
                           '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                           '<workbook xmlns="http://schemas.openxmlformats.org/'
                           'spreadsheetml/2006/main" xmlns:r="http://schemas.'
                           'openxmlformats.org/officeDocument/2006/relationships">'
                           "<sheets>%s</sheets></workbook>" % abas)
        zip_saida.writestr("xl/_rels/workbook.xml.rels",
                           '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                           '<Relationships xmlns="http://schemas.openxmlformats.org/'
                           'package/2006/relationships">%s</Relationships>' % rels)
        zip_saida.writestr("xl/styles.xml", ESTILOS)
        for i, (_nome, cabecalho, linhas, larguras) in enumerate(planilhas):
            zip_saida.writestr("xl/worksheets/sheet%d.xml" % (i + 1),
                               _planilha(cabecalho, linhas, larguras))
    return caminho
