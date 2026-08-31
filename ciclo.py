"""Cruza PRs abertos (GitHub) com work packages (OpenProject) e com o git local.

Regra do processo: certos tipos de tarefa - tipicamente corretiva e divida
tecnica - precisam chegar nas branches de versao (producao e homologacao), e nao
so na principal. Outros tipos ficam so na principal, a menos que a propria
tarefa peca disponibilizacao em outras versoes. Quais tipos exigem o que e
configuravel: nada aqui presume o processo de nenhuma empresa.

Quem decide em quais branches a tarefa TEM de estar e um lugar so:
branches_obrigatorias(). Situacao por branch, pendencia, coluna PENDENTE EM e
exportacao leem todas dessa mesma resposta - nao existe segunda regra.

Cada branch vira uma frase, nao um numero de PR:

    mergeado                 - a tarefa aparece no historico daquela branch
    aprovado, falta mergear  - PR aberto e ja aprovado na revisao
    comentado, sem aprovar   - alguem revisou e comentou, mas nao aprovou
    revisao pediu ajuste     - PR aberto com pedido de mudanca
    aguardando aprovacao     - PR aberto, sem revisao ainda
    rascunho                 - PR aberto como draft
    PR nao aberto            - nada aberto e nada no historico daquela branch
    sem PR aberto            - nada aberto e nao havia clone local para conferir
    nao solicitado           - a branch nao e obrigatoria para essa tarefa
"""

import datetime
import re
import unicodedata

MERGEAR = "PODE MERGEAR"
SEM_PROD = "SEM PR DE PRODUCAO"
SEM_VERSAO = "FALTA EM OUTRA VERSAO"
APROVAR = "AGUARDA APROVACAO"
SEM_BUILD = "FALTA A BUILD (X5)"
PARADO = "PARADO"
OK = "OK"

ORDEM_CICLO = {MERGEAR: 0, SEM_PROD: 1, SEM_VERSAO: 2, APROVAR: 3,
               SEM_BUILD: 4, PARADO: 5, OK: 6}

CORES_CICLO = {
    MERGEAR: "#0a7a0a",
    SEM_PROD: "#b00000",
    SEM_VERSAO: "#a00050",
    APROVAR: "#0a4fb0",
    SEM_BUILD: "#8a5a00",
    PARADO: "#b06000",
    OK: "#707070",
}

LEGENDA_CICLO = (
    (MERGEAR, "a tarefa esta num status de concluida e o PR continua aberto - e so mergear"),
    (SEM_PROD, "o tipo exige producao e nao ha PR aberto nem nada no historico da branch"),
    (SEM_VERSAO, "falta em outra branch obrigatoria (homologacao ou versao pedida na tarefa)"),
    (APROVAR, "existe PR aberto para uma branch obrigatoria esperando revisao ha N dias"),
    (SEM_BUILD, "tarefa concluida, sem PR aberto, e com o campo de build vazio"),
    (PARADO, "PR aberto sem nenhuma atualizacao ha mais dias que o limite"),
    (OK, "nada a fazer pelo que da para ver"),
)

# situacoes de cada branch
MERGEADO = "mergeado"
APROVADO = "aprovado, falta mergear"
AJUSTES = "revisao pediu ajuste"
AGUARDANDO = "aguardando aprovacao"
COMENTADO = "comentado, sem aprovar"
RASCUNHO = "rascunho"
SEM_PR = "PR nao aberto"
NAO_CONFERIDO = "sem PR aberto"
NAO_SOLICITADO = "nao solicitado"

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


def mesma_branch(um, outro):
    """Compara sem diferenciar maiusculas: a API do GitHub costuma devolver o nome
    da branch em minusculas onde o git local mostra em maiusculas."""
    return sem_acento(um) == sem_acento(outro)


# nome antigo, mantido: a comparacao e a mesma para qualquer branch
e_producao = mesma_branch


# ---------------------------------------------------------------- versoes

def versoes_do_texto(texto):
    """Versoes de 4 digitos citadas num texto livre, na ordem, sem repetir.

    O campo 'ramos para disponibilizacao' nao tem formato fixo: 'v2.2602',
    '2602 - 2601', '2602/2601' e '2602, 2601' aparecem todos. O unico padrao
    confiavel sao os 4 digitos, e e so isso que lemos - o separador nao importa.
    """
    achadas = []
    # str(): campo numerico no gerenciador volta como int, nao como texto
    for versao in re.findall(r"\b\d{4}\b", str(texto or "")):
        if versao not in achadas:
            achadas.append(versao)
    return achadas


def modelo_de_branch(*nomes):
    """Deriva o formato do nome de branch de exemplos: 'v2.2601' -> 'v2.%s'.

    Assim o '2602' que vem do OpenProject vira o nome de branch que ESTE
    repositorio usa, sem 'v2.' fixo no codigo. Sem exemplo, devolve '%s'.
    """
    for nome in nomes:
        achadas = list(re.finditer(r"\b\d{4}\b", str(nome or "")))
        if achadas:
            inicio, fim = achadas[-1].span()
            return nome[:inicio] + "%s" + nome[fim:]
    return "%s"


def branch_da_versao(versao, modelo="%s", conhecidas=()):
    """'2602' -> 'v2.2602'. Se uma branch conhecida ja e dessa versao, usa o nome
    dela: o que o usuario digitou vale mais que o modelo deduzido."""
    for nome in conhecidas:
        if nome and versao in versoes_do_texto(nome):
            return nome
    return modelo % versao


def verdadeiro(valor):
    """Booleano do OpenProject: aceita bool, 'sim'/'nao', 'true'/'false', 1/0."""
    if isinstance(valor, bool):
        return valor
    if valor is None:
        return False
    return sem_acento(str(valor)).strip() in (
        "1", "true", "sim", "yes", "y", "s", "verdadeiro", "on")


def branches_obrigatorias(tipo, dados, cfg):
    """Branches em que a tarefa TEM de estar. Regra unica da aplicacao.

    dados: o registro da tarefa ({"entrega", "ramos", ...}). cfg: o que esta
    configurado na tela ({"principal", "producao", "homologacao",
    "tipos_exigem"}).

        1. a principal (master) e sempre obrigatoria;
        2. tipo marcado em 'Tipos que exigem producao' -> producao e
           homologacao (a regra antiga, estendida da producao para as duas
           branches de versao configuradas);
        3. 'Confirmar entrega ao cliente?' verdadeiro -> as versoes citadas em
           'ramos para disponibilizacao', qualquer que seja o tipo.

    Versao que a tarefa nao pediu nao entra: e o que separa 'PR nao aberto' de
    'nao solicitado' na coluna daquela branch.
    """
    producao = (cfg.get("producao") or "").strip()
    homologacao = (cfg.get("homologacao") or "").strip()
    exigem = {sem_acento(t) for t in (cfg.get("tipos_exigem") or [])}
    modelo = cfg.get("modelo") or modelo_de_branch(producao, homologacao)

    obrigatorias = []

    def juntar(nome):
        if nome and not any(mesma_branch(nome, ja) for ja in obrigatorias):
            obrigatorias.append(nome)

    juntar((cfg.get("principal") or "").strip())
    if tipo and sem_acento(tipo) in exigem:
        juntar(producao)
        juntar(homologacao)
    if verdadeiro((dados or {}).get("entrega")):
        for versao in versoes_do_texto((dados or {}).get("ramos", "")):
            juntar(branch_da_versao(versao, modelo, (producao, homologacao)))
    return obrigatorias


# ---------------------------------------------------------------- situacao

def _dias_desde(texto_data, hoje):
    try:
        data = datetime.date(*[int(p) for p in texto_data.split("-")])
    except Exception:
        return 0
    return (hoje - data).days


def situacao_do_lado(prs_do_lado, revisoes, tarefa, historico, hoje, obrigatoria=True):
    """(frase, ja_esta_la) descrevendo uma branch.

    historico: numeros de tarefa vistos no historico daquela branch, ou None
    quando nao havia clone local para conferir. obrigatoria=False troca o
    'PR nao aberto' por 'nao solicitado': a ausencia ali nao e pendencia.
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
    if historico is not None and tarefa and tarefa in historico:
        return MERGEADO, True
    if not obrigatoria:
        return NAO_SOLICITADO, False
    if historico is None:
        return NAO_CONFERIDO, False
    return SEM_PR, False


def _lado(nome, prs_do_grupo, obrigatorias, revisoes, tarefa, historico, hoje):
    """Registro de uma branch: os PRs dela, se e obrigatoria e como esta."""
    prs = [p for p in prs_do_grupo if mesma_branch(p.get("base", ""), nome)]
    obrigatoria = any(mesma_branch(nome, o) for o in obrigatorias)
    texto, mergeado = situacao_do_lado(
        prs, revisoes, tarefa, historico.get(nome), hoje, obrigatoria)
    return {
        "nome": nome,
        "obrigatoria": obrigatoria,
        "situacao": texto,
        "mergeado": mergeado,
        "pendente": obrigatoria and not mergeado,
        "prs": prs,
        "urls": [p["url"] for p in prs if p.get("url")],
    }


def montar(prs, tarefas, base_producao, base_principal, tipos_exigem,
           status_libera, dias_parado=7, hoje=None, revisoes=None, historico=None,
           base_homologacao="", modelo_branch=""):
    """Agrupa os PRs abertos por tarefa e aponta em qual branch ela esta pendente.

    tarefas: {numero: {"tipo","status","fechado","assunto","build","entrega","ramos"}}.
    revisoes: {(repo, numero_pr): "aprovado"|"ajustes"|"comentado"|""}.
    historico: {nome_da_branch: set de numeros de tarefa}, None naquela branch
    quando nao havia clone local para conferir.
    """
    hoje = hoje or datetime.date.today()
    revisoes = revisoes or {}
    historico = historico or {}
    libera = {sem_acento(s) for s in status_libera}
    cfg = {
        "principal": base_principal,
        "producao": base_producao,
        "homologacao": base_homologacao,
        "tipos_exigem": tipos_exigem,
        "modelo": modelo_branch or modelo_de_branch(base_producao, base_homologacao),
    }
    # dict.fromkeys: se a mesma branch for digitada em dois campos, ela nao pode
    # virar duas colunas nem contar a pendencia duas vezes
    nomeadas = list(dict.fromkeys(
        n for n in (base_principal, base_producao, base_homologacao) if n))

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

        obrigatorias = branches_obrigatorias(tipo, dados, cfg)

        # as tres branches nomeadas tem coluna propria; as demais entram por
        # serem obrigatorias (ramos) ou por terem PR aberto (outras branches)
        extras = [n for n in obrigatorias
                  if not any(mesma_branch(n, x) for x in nomeadas)]
        for pr in prs_do_grupo:
            base = pr.get("base", "")
            if base and not any(mesma_branch(base, x) for x in nomeadas + extras):
                extras.append(base)

        lados = [_lado(n, prs_do_grupo, obrigatorias, revisoes, tarefa, historico, hoje)
                 for n in nomeadas + extras]
        por_nome = {l["nome"]: l for l in lados}
        lado_principal = por_nome.get(base_principal)
        lado_prod = por_nome.get(base_producao)
        lado_homo = por_nome.get(base_homologacao)
        lados_extra = [por_nome[n] for n in extras if n in por_nome]

        texto_prod = lado_prod["situacao"] if lado_prod else ""
        pendentes = [l for l in lados if l["pendente"]]
        # so a producao mantem a pendencia historica; as outras branches
        # obrigatorias caem em SEM_VERSAO, para o rotulo nao mentir
        prod_sem_pr = bool(lado_prod and lado_prod["pendente"] and not lado_prod["prs"])
        outras_sem_pr = [l for l in pendentes if not l["prs"]
                         and l is not lado_prod and l is not lado_principal]
        # PR aberto em branch de versao obrigatoria (a producao conta sempre,
        # como antes, mesmo quando o tipo nao a exige)
        prs_em_versao = [l for l in lados if l["prs"] and l is not lado_principal
                         and (l["obrigatoria"] or l is lado_prod)]

        idade = max((_dias_desde(p["atualizado"], hoje) for p in prs_do_grupo), default=0)
        idade_versao = max((_dias_desde(p["atualizado"], hoje)
                            for l in prs_em_versao for p in l["prs"]), default=0)
        concluida = dados.get("fechado") or (sem_acento(status_wp) in libera if libera else False)
        build = dados.get("build", "")

        if concluida and prs_do_grupo:
            pendencia = MERGEAR
            detalhe = ("tarefa em '%s' (status de concluida) com %d PR(s) ainda aberto(s)"
                       % (status_wp, len(prs_do_grupo)))
        elif prod_sem_pr:
            pendencia = SEM_PROD
            detalhe = "tipo '%s' exige producao e la esta '%s'" % (tipo, texto_prod)
        elif outras_sem_pr:
            pendencia = SEM_VERSAO
            detalhe = "falta em %s" % ", ".join(
                "%s (%s)" % (l["nome"], l["situacao"]) for l in outras_sem_pr)
        elif prs_em_versao:
            pendencia = APROVAR
            detalhe = "%s (ha %d dias)" % (
                ", ".join("%s: %s" % (l["nome"], l["situacao"]) for l in prs_em_versao),
                idade_versao)
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
            "entrega": verdadeiro(dados.get("entrega")),
            "tem_entrega": dados.get("entrega") is not None,
            "ramos": dados.get("ramos", "") or "",
            "versoes": versoes_do_texto(dados.get("ramos", "")),
            "obrigatorias": [l["nome"] for l in lados if l["obrigatoria"]],
            "branches": lados,
            "pendente_em": ", ".join("%s: %s" % (l["nome"], l["situacao"]) for l in pendentes),
            "pr_principal": lado_principal["situacao"] if lado_principal else "",
            "pr_producao": texto_prod,
            "pr_homologacao": lado_homo["situacao"] if lado_homo else "",
            "pr_outros": ", ".join("#%s(%s)" % (p["numero"], p["base"])
                                   for l in lados_extra for p in l["prs"]),
            "autores": sorted({p["autor"] for p in prs_do_grupo}),
            "idade": idade,
            "pendencia": pendencia,
            "detalhe": detalhe,
            "prs": prs_do_grupo,
            # links por coluna, para o clique na celula abrir no navegador
            "urls": {
                "principal": lado_principal["urls"] if lado_principal else [],
                "producao": lado_prod["urls"] if lado_prod else [],
                "homologacao": lado_homo["urls"] if lado_homo else [],
                "outros": [u for l in lados_extra for u in l["urls"]],
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
        obrigatorias = branches_obrigatorias(dados.get("tipo", ""), dados, cfg)
        nomes = list(dict.fromkeys(nomeadas + obrigatorias))
        lados = [_lado(n, [], obrigatorias, revisoes, numero, historico, hoje) for n in nomes]
        por_nome = {l["nome"]: l for l in lados}
        pendentes = [l for l in lados if l["pendente"]]
        linhas.append({
            "tarefa": numero, "chave": numero,
            "tipo": dados.get("tipo", "-"), "origem_tipo": "gerenciador",
            "status_wp": dados.get("status", "-"), "build": "",
            "assunto": dados.get("assunto", ""),
            "entrega": verdadeiro(dados.get("entrega")),
            "tem_entrega": True,
            "ramos": dados.get("ramos", "") or "",
            "versoes": versoes_do_texto(dados.get("ramos", "")),
            "obrigatorias": [l["nome"] for l in lados if l["obrigatoria"]],
            "branches": lados,
            "pendente_em": ", ".join("%s: %s" % (l["nome"], l["situacao"]) for l in pendentes),
            "pr_principal": (por_nome[base_principal]["situacao"]
                             if base_principal in por_nome else ""),
            "pr_producao": (por_nome[base_producao]["situacao"]
                            if base_producao in por_nome else ""),
            "pr_homologacao": (por_nome[base_homologacao]["situacao"]
                               if base_homologacao in por_nome else ""),
            "pr_outros": "",
            "autores": [], "idade": 0,
            "pendencia": SEM_BUILD,
            "detalhe": "tarefa concluida e sem o campo de build preenchido",
            "prs": [],
            "urls": {"principal": [], "producao": [], "homologacao": [], "outros": []},
        })

    linhas.sort(key=lambda x: (ORDEM_CICLO.get(x["pendencia"], 9), -x["idade"]))
    return linhas


# ---------------------------------------------------------------- formato longo

COLUNAS_LONGO = ("TAREFA", "TIPO", "STATUS", "BRANCH", "OBRIGATORIA", "SITUACAO",
                 "PENDENTE", "PENDENCIA", "ENTREGA AO CLIENTE", "RAMOS", "ASSUNTO")


def linhas_por_branch(linhas):
    """Uma linha por tarefa x branch - o formato que o Excel filtra e resume bem."""
    saida = []
    for linha in linhas:
        for lado in linha["branches"]:
            saida.append([
                linha["tarefa"], linha["tipo"], linha["status_wp"], lado["nome"],
                "sim" if lado["obrigatoria"] else "nao", lado["situacao"],
                "sim" if lado["pendente"] else "nao", linha["pendencia"],
                ("sim" if linha["entrega"] else "nao") if linha.get("tem_entrega") else "-",
                linha["ramos"] or "-", linha["assunto"],
            ])
    return saida
