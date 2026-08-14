"""Cruza PRs abertos (GitHub) com work packages (OpenProject) e com o git local.

Regra do processo: certos tipos de tarefa - tipicamente corretiva e divida
tecnica - precisam chegar na branch de producao, e nao so na principal. Outros
tipos ficam so na principal. Quais tipos exigem o que e configuravel: nada aqui
presume o processo de nenhuma empresa.

Cada lado (principal e producao) vira uma frase, nao um numero de PR:

    mergeado                 - a tarefa aparece no historico daquela branch
    aprovado, falta mergear  - PR aberto e ja aprovado na revisao
    comentado, sem aprovar   - alguem revisou e comentou, mas nao aprovou
    revisao pediu ajuste     - PR aberto com pedido de mudanca
    aguardando aprovacao     - PR aberto, sem revisao ainda
    rascunho                 - PR aberto como draft
    PR nao aberto            - nada aberto e nada no historico daquela branch
    sem PR aberto            - nada aberto e nao havia clone local para conferir
"""

import datetime
import re
import unicodedata

MERGEAR = "PODE MERGEAR"
SEM_PROD = "SEM PR DE PRODUCAO"
APROVAR = "AGUARDA APROVACAO"
SEM_BUILD = "FALTA A BUILD (X5)"
PARADO = "PARADO"
OK = "OK"

ORDEM_CICLO = {MERGEAR: 0, SEM_PROD: 1, APROVAR: 2, SEM_BUILD: 3, PARADO: 4, OK: 5}

CORES_CICLO = {
    MERGEAR: "#0a7a0a",
    SEM_PROD: "#b00000",
    APROVAR: "#0a4fb0",
    SEM_BUILD: "#8a5a00",
    PARADO: "#b06000",
    OK: "#707070",
}

LEGENDA_CICLO = (
    (MERGEAR, "a tarefa esta num status de concluida e o PR continua aberto - e so mergear"),
    (SEM_PROD, "o tipo exige producao e nao ha PR aberto nem nada no historico da branch"),
    (APROVAR, "existe PR aberto para a producao esperando revisao/aprovacao ha N dias"),
    (SEM_BUILD, "tarefa concluida, sem PR aberto, e com o campo de build vazio"),
    (PARADO, "PR aberto sem nenhuma atualizacao ha mais dias que o limite"),
    (OK, "nada a fazer pelo que da para ver"),
)

# situacoes de cada lado
MERGEADO = "mergeado"
APROVADO = "aprovado, falta mergear"
AJUSTES = "revisao pediu ajuste"
AGUARDANDO = "aguardando aprovacao"
COMENTADO = "comentado, sem aprovar"
RASCUNHO = "rascunho"
SEM_PR = "PR nao aberto"
NAO_CONFERIDO = "sem PR aberto"

# tipos de manutencao mais comuns, usados so como reserva quando a tarefa nao
# esta no gerenciador: o titulo do PR costuma trazer a natureza.
TIPOS_CONHECIDOS = ("divida tecnica", "corretiva", "adaptativa", "evolutiva", "regressao")


def sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in texto if not unicodedata.combining(c)).lower()


def tarefa_do_pr(pr):
    """Numero da tarefa a partir do branch (fb_123456_2601) ou do titulo."""
    for origem in (pr.get("head", ""), pr.get("titulo", "")):
        limpo = re.sub(r"\(#\d+\)", " ", origem)
        achado = re.findall(r"\b\d{5,7}\b", limpo)
        if achado:
            return achado[0]
    return ""


def tipo_do_titulo(titulo):
    """Natureza deduzida do titulo do PR. '' se nao reconhecer."""
    limpo = sem_acento(titulo)
    for tipo in TIPOS_CONHECIDOS:
        if tipo in limpo:
            return tipo
    return ""


def e_producao(base, base_producao):
    """Compara sem diferenciar maiusculas: a API do GitHub costuma devolver o nome
    da branch em minusculas onde o git local mostra em maiusculas."""
    return sem_acento(base) == sem_acento(base_producao)


def _dias_desde(texto_data, hoje):
    try:
        data = datetime.date(*[int(p) for p in texto_data.split("-")])
    except Exception:
        return 0
    return (hoje - data).days


def situacao_do_lado(prs_do_lado, revisoes, tarefa, historico, hoje):
    """(frase, ja_esta_la) descrevendo um lado - principal ou producao.

    historico: numeros de tarefa vistos no historico daquela branch, ou None
    quando nao havia clone local para conferir.
    """
    if prs_do_lado:
        partes = []
        for pr in prs_do_lado:
            estado = revisoes.get((pr.get("repo"), pr.get("numero")), "")
            if pr.get("rascunho"):
                texto = RASCUNHO
            elif estado == "aprovado":
                texto = APROVADO
            elif estado == "ajustes":
                texto = AJUSTES
            elif estado == "comentado":
                texto = "%s (%dd)" % (COMENTADO, _dias_desde(pr.get("atualizado", ""), hoje))
            else:
                texto = "%s (%dd)" % (AGUARDANDO, _dias_desde(pr.get("atualizado", ""), hoje))
            partes.append("%s #%s" % (texto, pr.get("numero")))
        return ", ".join(partes), False
    if historico is None:
        return NAO_CONFERIDO, False
    if tarefa and tarefa in historico:
        return MERGEADO, True
    return SEM_PR, False


def montar(prs, tarefas, base_producao, base_principal, tipos_exigem,
           status_libera, dias_parado=7, hoje=None, revisoes=None, historico=None):
    """Agrupa os PRs abertos por tarefa e aponta a pendencia de cada uma.

    tarefas: {numero: {"tipo","status","fechado","assunto","build"}} do gerenciador.
    revisoes: {(repo, numero_pr): "aprovado"|"ajustes"|""}.
    historico: {"principal": set|None, "producao": set|None}.
    """
    hoje = hoje or datetime.date.today()
    revisoes = revisoes or {}
    historico = historico or {}
    exigem = {sem_acento(t) for t in tipos_exigem}
    libera = {sem_acento(s) for s in status_libera}

    grupos = {}
    for pr in prs:
        tarefa = tarefa_do_pr(pr)
        chave = tarefa or ("pr:%s#%s" % (pr.get("repo"), pr.get("numero")))
        grupo = grupos.setdefault(chave, {"tarefa": tarefa, "prs": []})
        grupo["prs"].append(pr)

    linhas = []
    for chave, grupo in grupos.items():
        prs_do_grupo = grupo["prs"]
        tarefa = grupo["tarefa"]
        dados = tarefas.get(tarefa, {})
        tipo = dados.get("tipo") or tipo_do_titulo(prs_do_grupo[0].get("titulo", ""))
        origem_tipo = "gerenciador" if dados.get("tipo") else (
            "titulo do PR" if tipo else "nao identificado")
        status_wp = dados.get("status", "")

        para_prod = [p for p in prs_do_grupo if e_producao(p["base"], base_producao)]
        para_principal = [p for p in prs_do_grupo
                          if sem_acento(p["base"]) == sem_acento(base_principal)]
        outros = [p for p in prs_do_grupo if p not in para_prod and p not in para_principal]

        texto_principal, _ = situacao_do_lado(
            para_principal, revisoes, tarefa, historico.get("principal"), hoje)
        texto_prod, prod_mergeado = situacao_do_lado(
            para_prod, revisoes, tarefa, historico.get("producao"), hoje)

        idade = max((_dias_desde(p["atualizado"], hoje) for p in prs_do_grupo), default=0)
        idade_prod = max((_dias_desde(p["atualizado"], hoje) for p in para_prod), default=0)
        exige_producao = sem_acento(tipo) in exigem if tipo else False
        concluida = dados.get("fechado") or (sem_acento(status_wp) in libera if libera else False)
        build = dados.get("build", "")

        if concluida and prs_do_grupo:
            pendencia = MERGEAR
            detalhe = ("tarefa em '%s' (status de concluida) com %d PR(s) ainda aberto(s)"
                       % (status_wp, len(prs_do_grupo)))
        elif exige_producao and not para_prod and not prod_mergeado:
            pendencia = SEM_PROD
            detalhe = "tipo '%s' exige producao e la esta '%s'" % (tipo, texto_prod)
        elif para_prod:
            pendencia = APROVAR
            detalhe = "producao: %s (ha %d dias)" % (texto_prod, idade_prod)
        elif concluida and not prs_do_grupo and not build:
            pendencia = SEM_BUILD
            detalhe = "tarefa concluida e sem o campo de build preenchido"
        elif idade >= dias_parado:
            pendencia = PARADO
            detalhe = "sem atualizacao ha %d dias" % idade
        else:
            pendencia = OK
            detalhe = ""

        linhas.append({
            "tarefa": tarefa or "-",
            "chave": chave,
            "tipo": tipo or "-",
            "origem_tipo": origem_tipo,
            "status_wp": status_wp or "-",
            "build": dados.get("build", ""),
            "assunto": dados.get("assunto") or prs_do_grupo[0].get("titulo", ""),
            "pr_principal": texto_principal,
            "pr_producao": texto_prod,
            "pr_outros": ", ".join("#%s(%s)" % (p["numero"], p["base"]) for p in outros),
            "autores": sorted({p["autor"] for p in prs_do_grupo}),
            "idade": idade,
            "pendencia": pendencia,
            "detalhe": detalhe,
            "prs": prs_do_grupo,
            # links por coluna, para o clique na celula abrir no navegador
            "urls": {
                "principal": [p["url"] for p in para_principal if p.get("url")],
                "producao": [p["url"] for p in para_prod if p.get("url")],
                "outros": [p["url"] for p in outros if p.get("url")],
            },
        })

    # tarefas sem PR aberto (vieram da query salva) so entram se apontarem pendencia:
    # concluidas e sem build preenchido - o "ja foi feito e o X5 ficou vazio"
    for numero, dados in tarefas.items():
        if numero in grupos:
            continue
        concluida = dados.get("fechado") or (
            sem_acento(dados.get("status", "")) in libera if libera else False)
        if not (concluida and not dados.get("build")):
            continue
        principal = historico.get("principal")
        producao = historico.get("producao")
        linhas.append({
            "tarefa": numero, "chave": numero,
            "tipo": dados.get("tipo", "-"), "origem_tipo": "gerenciador",
            "status_wp": dados.get("status", "-"), "build": "",
            "assunto": dados.get("assunto", ""),
            "pr_principal": MERGEADO if principal and numero in principal else NAO_CONFERIDO,
            "pr_producao": MERGEADO if producao and numero in producao else NAO_CONFERIDO,
            "pr_outros": "",
            "autores": [], "idade": 0,
            "pendencia": SEM_BUILD,
            "detalhe": "tarefa concluida e sem o campo de build preenchido",
            "prs": [], "urls": {"principal": [], "producao": [], "outros": []},
        })

    linhas.sort(key=lambda x: (ORDEM_CICLO.get(x["pendencia"], 9), -x["idade"]))
    return linhas
