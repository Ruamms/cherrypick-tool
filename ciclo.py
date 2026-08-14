"""Cruza PRs abertos (GitHub) com work packages (OpenProject).

Regra do processo: certos tipos de tarefa - tipicamente corretiva e divida
tecnica - precisam chegar na branch de producao, e nao so na principal. Outros
tipos ficam so na principal. Quais tipos exigem o que e configuravel: nada aqui
presume o processo de nenhuma empresa.

Escopo desta versao: **so PR aberto**. Se ninguem abriu o PR de producao, aparece
como pendencia; se o PR ja foi mergeado, ele sai do radar - por isso a pendencia
se chama "sem PR aberto para producao", e nao "nao esta na producao".
"""

import datetime
import re
import unicodedata

MERGEAR = "PODE MERGEAR"
SEM_PROD = "SEM PR DE PRODUCAO"
PARADO = "PARADO"
OK = "OK"

ORDEM_CICLO = {MERGEAR: 0, SEM_PROD: 1, PARADO: 2, OK: 3}

CORES_CICLO = {
    MERGEAR: "#0a7a0a",
    SEM_PROD: "#b00000",
    PARADO: "#b06000",
    OK: "#707070",
}

LEGENDA_CICLO = (
    (MERGEAR, "a tarefa ja passou no teste e o PR continua aberto - da para mergear agora"),
    (SEM_PROD, "o tipo exige producao e nao ha PR aberto para a branch de producao"),
    (PARADO, "PR aberto sem nenhuma atualizacao ha mais dias que o limite"),
    (OK, "nada a fazer pelo que da para ver dos PRs abertos"),
)

# tipos de manutencao mais comuns, usados so como reserva quando a tarefa nao
# esta na query do OpenProject: o titulo do PR costuma trazer a natureza.
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


def montar(prs, tarefas, base_producao, base_principal, tipos_exigem,
           status_libera, dias_parado=7, hoje=None):
    """Agrupa os PRs abertos por tarefa e aponta a pendencia de cada uma.

    tarefas: {numero: {"tipo","status","assunto","build"}} vindo do OpenProject.
    tipos_exigem / status_libera: nomes escolhidos pelo usuario (sem acento, minusculo).
    """
    hoje = hoje or datetime.date.today()
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
        origem_tipo = "OpenProject" if dados.get("tipo") else (
            "titulo do PR" if tipo else "nao identificado")
        status_wp = dados.get("status", "")

        para_prod = [p for p in prs_do_grupo if e_producao(p["base"], base_producao)]
        para_principal = [p for p in prs_do_grupo if sem_acento(p["base"]) == sem_acento(base_principal)]
        outros = [p for p in prs_do_grupo if p not in para_prod and p not in para_principal]

        idade = max((_dias_desde(p["atualizado"], hoje) for p in prs_do_grupo), default=0)
        exige_producao = sem_acento(tipo) in exigem if tipo else False

        if libera and sem_acento(status_wp) in libera:
            pendencia = MERGEAR
            detalhe = "tarefa em '%s' com %d PR(s) aberto(s)" % (status_wp, len(prs_do_grupo))
        elif exige_producao and not para_prod:
            pendencia = SEM_PROD
            detalhe = ("tipo '%s' exige producao e nao ha PR aberto para %s "
                       "(pode ja ter sido mergeado)" % (tipo, base_producao))
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
            "pr_principal": ", ".join("#%s" % p["numero"] for p in para_principal) or "-",
            "pr_producao": ", ".join("#%s" % p["numero"] for p in para_prod) or "-",
            "pr_outros": ", ".join("#%s(%s)" % (p["numero"], p["base"]) for p in outros),
            "autores": sorted({p["autor"] for p in prs_do_grupo}),
            "idade": idade,
            "pendencia": pendencia,
            "detalhe": detalhe,
            "prs": prs_do_grupo,
        })

    linhas.sort(key=lambda x: (ORDEM_CICLO.get(x["pendencia"], 9), -x["idade"]))
    return linhas
