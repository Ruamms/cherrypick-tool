"""Testes da regra do ciclo. Rode com: python -m unittest -v

So stdlib, como o resto do projeto. Metade dos testes existe para travar o
comportamento ANTIGO (uma branch de producao, sem os campos novos do
OpenProject): a evolucao para multiplas branches nao pode mudar o que a
ferramenta ja respondia.
"""

import datetime
import os
import unittest
import xml.etree.ElementTree as ET
import zipfile

import ciclo
import excel

HOJE = datetime.date(2026, 8, 31)
MAIN = "master"
PROD = "v2.2601"
HOMO = "v2.2602"


def pr(numero, base, tarefa="", atualizado="2026-08-29", autor="ruan",
       rascunho=False, titulo="", repo="org/repo"):
    return {
        "repo": repo, "numero": numero,
        "titulo": titulo or ("Ajuste - tarefa %s" % tarefa),
        "base": base, "head": ("fb_%s_%s" % (tarefa, base[-4:])) if tarefa else "algo",
        "autor": autor, "criado": "2026-08-01", "atualizado": atualizado,
        "rascunho": rascunho, "url": "https://github.com/org/repo/pull/%s" % numero,
    }


def tarefa(tipo, status="Desenvolvido", fechado=False, build="", entrega=None,
           ramos="", assunto="Assunto da tarefa"):
    return {"tipo": tipo, "status": status, "fechado": fechado, "assunto": assunto,
            "build": build, "entrega": entrega, "ramos": ramos}


def uma(linhas, numero):
    return next(l for l in linhas if l["tarefa"] == numero)


def situacao(linha, branch):
    return next(l["situacao"] for l in linha["branches"] if l["nome"] == branch)


class TestVersoes(unittest.TestCase):
    def test_formatos_do_campo_ramos(self):
        casos = {
            "v2.2602": ["2602"],
            "2602": ["2602"],
            "v2.2602 - v2.2601": ["2602", "2601"],
            "2602 - 2601": ["2602", "2601"],
            "2602, 2601": ["2602", "2601"],
            "2602/2601": ["2602", "2601"],
            "  2602  2601 2602 ": ["2602", "2601"],
            "": [],
            "sem versao aqui": [],
        }
        for texto, esperado in casos.items():
            self.assertEqual(ciclo.versoes_do_texto(texto), esperado, texto)

    def test_nao_confunde_com_numero_de_tarefa(self):
        # 5 a 7 digitos e tarefa, nao versao: nada de recorte no meio do numero
        self.assertEqual(ciclo.versoes_do_texto("tarefa 108692"), [])

    def test_modelo_sai_da_branch_configurada(self):
        self.assertEqual(ciclo.modelo_de_branch("v2.2601"), "v2.%s")
        self.assertEqual(ciclo.modelo_de_branch("release-2601"), "release-%s")
        self.assertEqual(ciclo.modelo_de_branch("", "v2.2602"), "v2.%s")
        self.assertEqual(ciclo.modelo_de_branch("master"), "%s")

    def test_branch_da_versao_prefere_o_nome_configurado(self):
        self.assertEqual(ciclo.branch_da_versao("2602", "v2.%s"), "v2.2602")
        self.assertEqual(
            ciclo.branch_da_versao("2601", "v2.%s", ("V2.2601", "v2.2602")), "V2.2601")

    def test_booleano_tolerante(self):
        for valor in (True, "sim", "Sim", "true", 1, "S"):
            self.assertTrue(ciclo.verdadeiro(valor), valor)
        for valor in (False, "nao", "Não", "false", 0, "", None):
            self.assertFalse(ciclo.verdadeiro(valor), valor)


class TestBranchesObrigatorias(unittest.TestCase):
    def cfg(self, exigem=("Corretiva", "Divida Tecnica"), homo=HOMO):
        return {"principal": MAIN, "producao": PROD, "homologacao": homo,
                "tipos_exigem": list(exigem)}

    def test_regra_base_e_so_a_principal(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Evolutiva", tarefa("Evolutiva"), self.cfg()),
            [MAIN])

    def test_tipo_que_exige_producao_leva_as_duas_versoes(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", tarefa("Corretiva"), self.cfg()),
            [MAIN, PROD, HOMO])

    def test_sem_homologacao_configurada_e_a_regra_antiga(self):
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", tarefa("Corretiva"),
                                        self.cfg(homo="")),
            [MAIN, PROD])

    def test_entrega_ao_cliente_adiciona_os_ramos(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2602 / 2601")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()),
            [MAIN, HOMO, PROD])

    def test_entrega_ao_cliente_com_um_ramo_so(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2602")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()), [MAIN, HOMO])

    def test_entrega_falsa_nao_adiciona_nada(self):
        dados = tarefa("Adaptativa", entrega=False, ramos="2602")
        self.assertEqual(
            ciclo.branches_obrigatorias("Adaptativa", dados, self.cfg()), [MAIN])

    def test_corretiva_com_ramos_nao_duplica_a_producao(self):
        dados = tarefa("Corretiva", entrega=True, ramos="2601")
        self.assertEqual(
            ciclo.branches_obrigatorias("Corretiva", dados, self.cfg()),
            [MAIN, PROD, HOMO])


class TestRegrasAntigas(unittest.TestCase):
    """Sem homologacao e sem os campos novos, a saida tem de ser a de antes."""

    def montar(self, prs, tarefas, **kw):
        kw.setdefault("historico", {MAIN: set(), PROD: set()})
        return ciclo.montar(prs, tarefas, PROD, MAIN, ["Corretiva", "Divida Tecnica"],
                            [], dias_parado=7, hoje=HOJE, **kw)

    def test_corretiva_sem_pr_de_producao(self):
        linhas = self.montar([pr(8006, MAIN, "108692")],
                             {"108692": tarefa("Corretiva")})
        linha = uma(linhas, "108692")
        self.assertEqual(linha["pendencia"], ciclo.SEM_PROD)
        self.assertEqual(linha["pr_producao"], ciclo.SEM_PR)

    def test_evolutiva_nao_e_cobrada_por_producao(self):
        linhas = self.montar([pr(8085, MAIN, "109757", atualizado="2026-08-30")],
                             {"109757": tarefa("Evolutiva")})
        self.assertEqual(uma(linhas, "109757")["pendencia"], ciclo.OK)

    def test_pr_de_producao_aberto_e_aguarda_aprovacao(self):
        linhas = self.montar([pr(7973, PROD, "109453", atualizado="2026-07-24")],
                             {"109453": tarefa("Divida Tecnica")})
        linha = uma(linhas, "109453")
        self.assertEqual(linha["pendencia"], ciclo.APROVAR)
        self.assertIn("aguardando aprovacao", linha["pr_producao"])

    def test_concluida_com_pr_aberto_pode_mergear(self):
        linhas = self.montar([pr(8030, MAIN, "109866")],
                             {"109866": tarefa("Adaptativa", status="Testado",
                                               fechado=True)})
        self.assertEqual(uma(linhas, "109866")["pendencia"], ciclo.MERGEAR)

    def test_parado_por_idade(self):
        linhas = self.montar([pr(8010, MAIN, atualizado="2026-08-04")], {})
        self.assertEqual(linhas[0]["pendencia"], ciclo.PARADO)
        self.assertEqual(linhas[0]["idade"], 27)

    def test_falta_build_sem_pr_aberto(self):
        linhas = self.montar([], {"104328": tarefa("Adaptativa", fechado=True)})
        linha = uma(linhas, "104328")
        self.assertEqual(linha["pendencia"], ciclo.SEM_BUILD)
        self.assertEqual(linha["prs"], [])

    def test_mergeado_vem_do_historico_local(self):
        linhas = self.montar([pr(7857, MAIN, "108241")],
                             {"108241": tarefa("Corretiva")},
                             historico={MAIN: {"108241"}, PROD: {"108241"}})
        self.assertEqual(uma(linhas, "108241")["pr_producao"], ciclo.MERGEADO)

    def test_sem_clone_local_nao_afirma_nada(self):
        linhas = self.montar([pr(7857, MAIN, "108241")],
                             {"108241": tarefa("Corretiva")},
                             historico={MAIN: None, PROD: None})
        self.assertEqual(uma(linhas, "108241")["pr_producao"], ciclo.NAO_CONFERIDO)

    def test_rascunho_e_revisao(self):
        prs = [pr(1, PROD, "100001"), pr(2, PROD, "100002", rascunho=True)]
        linhas = ciclo.montar(prs, {}, PROD, MAIN, [], [], hoje=HOJE,
                              revisoes={("org/repo", 1): "aprovado"},
                              historico={MAIN: set(), PROD: set()})
        self.assertIn(ciclo.APROVADO, uma(linhas, "100001")["pr_producao"])
        self.assertIn(ciclo.RASCUNHO, uma(linhas, "100002")["pr_producao"])

    def test_agrupa_as_duas_pontas_da_mesma_tarefa(self):
        linhas = self.montar([pr(7857, MAIN, "108241"), pr(7860, PROD, "108241")],
                             {"108241": tarefa("Corretiva")})
        self.assertEqual(len(linhas), 1)
        self.assertEqual(len(linhas[0]["prs"]), 2)

    def test_pr_em_branch_estranha_cai_em_outros(self):
        linhas = self.montar([pr(7860, "v2.2502", "108241")],
                             {"108241": tarefa("Corretiva")})
        self.assertIn("#7860(v2.2502)", uma(linhas, "108241")["pr_outros"])


class TestMultiplasBranches(unittest.TestCase):
    def montar(self, prs, tarefas, historico=None, exigem=("Corretiva", "Divida Tecnica")):
        historico = historico or {MAIN: set(), PROD: set(), HOMO: set()}
        return ciclo.montar(prs, tarefas, PROD, MAIN, list(exigem), [],
                            dias_parado=7, hoje=HOJE, historico=historico,
                            base_homologacao=HOMO)

    def test_corretiva_e_cobrada_nas_duas_versoes(self):
        """Item 6: 108692 corretiva, PR na 2601 aguardando, nada na 2602."""
        linhas = self.montar([pr(8006, PROD, "108692", atualizado="2026-07-30")],
                             {"108692": tarefa("Corretiva")})
        linha = uma(linhas, "108692")
        self.assertEqual(linha["obrigatorias"], [MAIN, PROD, HOMO])
        self.assertIn("aguardando aprovacao", situacao(linha, PROD))
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn("v2.2602: PR nao aberto", linha["pendente_em"])

    def test_adaptativa_com_entrega_e_dois_ramos(self):
        """Item 13: 109866 adaptativa, entrega=True, ramos 2602/2601."""
        linhas = self.montar(
            [pr(8030, PROD, "109866", atualizado="2026-08-12")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602 / 2601")},
            historico={MAIN: {"109866"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "109866")
        self.assertEqual(sorted(linha["obrigatorias"]), [MAIN, PROD, HOMO])
        self.assertIn("aguardando aprovacao", situacao(linha, PROD))
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertNotIn(MAIN, linha["pendente_em"])
        self.assertIn("v2.2601: aguardando", linha["pendente_em"])
        self.assertIn("v2.2602: PR nao aberto", linha["pendente_em"])

    def test_evolutiva_sem_entrega_nao_cria_pendencia_de_versao(self):
        """Item 14: 109757 evolutiva, entrega=False -> so a principal."""
        linhas = self.montar(
            [pr(8085, MAIN, "109757", atualizado="2026-08-26")],
            {"109757": tarefa("Evolutiva", entrega=False)})
        linha = uma(linhas, "109757")
        self.assertEqual(linha["obrigatorias"], [MAIN])
        self.assertEqual(situacao(linha, PROD), ciclo.NAO_SOLICITADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.NAO_SOLICITADO)
        self.assertEqual(linha["pendencia"], ciclo.OK)

    def test_entrega_so_na_homologacao_marca_producao_como_nao_solicitada(self):
        """Item 15: ramos 2602 -> a 2601 nao e cobrada."""
        linhas = self.montar(
            [pr(8100, MAIN, "108777")],
            {"108777": tarefa("Adaptativa", entrega=True, ramos="2602")},
            historico={MAIN: {"108777"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "108777")
        self.assertEqual(linha["obrigatorias"], [MAIN, HOMO])
        self.assertEqual(situacao(linha, PROD), ciclo.NAO_SOLICITADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        # o PR aberto na principal tambem e pendencia: ainda falta mergear
        self.assertIn("v2.2602: PR nao aberto", linha["pendente_em"])
        self.assertNotIn(PROD, linha["pendente_em"])

    def test_mergeado_em_uma_e_faltando_na_outra(self):
        """Item 7: 2601 mergeado, 2602 sem PR -> pendencia so na 2602.

        Sem nenhum PR aberto a tarefa so entra pela regra do build vazio - e
        mesmo por esse caminho ela tem de trazer a situacao de cada branch.
        """
        linhas = self.montar(
            [], {"108692": tarefa("Corretiva", fechado=True, build="")},
            historico={MAIN: {"108692"}, PROD: {"108692"}, HOMO: set()})
        linha = uma(linhas, "108692")
        self.assertEqual(situacao(linha, MAIN), ciclo.MERGEADO)
        self.assertEqual(situacao(linha, PROD), ciclo.MERGEADO)
        self.assertEqual(situacao(linha, HOMO), ciclo.SEM_PR)
        self.assertEqual(linha["pendente_em"], "v2.2602: PR nao aberto")

    def test_ramo_fora_das_branches_nomeadas(self):
        dados = tarefa("Adaptativa", entrega=True, ramos="2502")
        linhas = self.montar([pr(8200, MAIN, "108888")], {"108888": dados},
                             historico={MAIN: {"108888"}, PROD: set(), HOMO: set(),
                                        "v2.2502": set()})
        linha = uma(linhas, "108888")
        self.assertIn("v2.2502", linha["obrigatorias"])
        self.assertEqual(linha["pendencia"], ciclo.SEM_VERSAO)
        self.assertIn("v2.2502: PR nao aberto", linha["pendente_em"])

    def test_pr_de_homologacao_aberto_aguarda_aprovacao(self):
        linhas = self.montar(
            [pr(8300, HOMO, "109900", atualizado="2026-08-01")],
            {"109900": tarefa("Adaptativa", entrega=True, ramos="2602")},
            historico={MAIN: {"109900"}, PROD: set(), HOMO: set()})
        linha = uma(linhas, "109900")
        self.assertEqual(linha["pendencia"], ciclo.APROVAR)
        self.assertIn("aguardando aprovacao", linha["pr_homologacao"])

    def test_ramos_com_versao_ilegivel_nao_inventa_branch(self):
        linhas = self.montar(
            [pr(8400, MAIN, "109901")],
            {"109901": tarefa("Adaptativa", entrega=True, ramos="proxima release")},
            historico={MAIN: {"109901"}, PROD: set(), HOMO: set()})
        self.assertEqual(uma(linhas, "109901")["obrigatorias"], [MAIN])

    def test_campo_ausente_no_openproject_nao_quebra(self):
        linhas = self.montar([pr(8500, MAIN, "109902")],
                             {"109902": {"tipo": "Corretiva", "status": "Desenvolvido"}})
        linha = uma(linhas, "109902")
        self.assertFalse(linha["entrega"])
        self.assertEqual(linha["versoes"], [])
        self.assertEqual(linha["obrigatorias"], [MAIN, PROD, HOMO])


class TestFormatoLongo(unittest.TestCase):
    def test_uma_linha_por_tarefa_e_branch(self):
        linhas = ciclo.montar(
            [pr(8030, PROD, "109866")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602 - 2601")},
            PROD, MAIN, [], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        longo = ciclo.linhas_por_branch(linhas)
        self.assertEqual(len(longo), 3)
        self.assertEqual([l[3] for l in longo], [MAIN, PROD, HOMO])
        self.assertEqual(len(longo[0]), len(ciclo.COLUNAS_LONGO))
        por_branch = {l[3]: l for l in longo}
        self.assertEqual(por_branch[HOMO][4], "sim")     # obrigatoria
        self.assertEqual(por_branch[HOMO][6], "sim")     # pendente
        self.assertEqual(por_branch[HOMO][8], "sim")     # entrega ao cliente


class TestContratoDaLinha(unittest.TestCase):
    """A grade e a exportacao leem estas chaves de toda linha, nos dois caminhos
    que produzem linha: agrupada por PR aberto e a que entra pelo build vazio."""

    CHAVES = ("pendencia", "pendente_em", "tarefa", "tipo", "status_wp", "entrega",
              "tem_entrega", "versoes", "ramos", "obrigatorias", "branches",
              "pr_principal", "pr_producao", "pr_homologacao", "pr_outros",
              "build", "idade", "assunto", "detalhe", "autores", "prs", "urls")

    def test_as_duas_origens_de_linha_tem_todas_as_chaves(self):
        linhas = ciclo.montar(
            [pr(8030, PROD, "109866")],
            {"109866": tarefa("Adaptativa", entrega=True, ramos="2602"),
             "104328": tarefa("Adaptativa", fechado=True)},
            PROD, MAIN, ["Corretiva"], [], hoje=HOJE, base_homologacao=HOMO,
            historico={MAIN: set(), PROD: set(), HOMO: set()})
        self.assertEqual(len(linhas), 2)
        for linha in linhas:
            faltando = [c for c in self.CHAVES if c not in linha]
            self.assertEqual(faltando, [], "%s: %s" % (linha["tarefa"], faltando))
            for lado in ("principal", "producao", "homologacao", "outros"):
                self.assertIsInstance(linha["urls"][lado], list)
            for branch in linha["branches"]:
                self.assertEqual(sorted(branch), ["mergeado", "nome", "obrigatoria",
                                                  "pendente", "prs", "situacao", "urls"])


class TestExcel(unittest.TestCase):
    def setUp(self):
        self.caminho = os.path.join(os.environ.get("TEMP", "."), "teste-ciclo.xlsx")

    def tearDown(self):
        if os.path.exists(self.caminho):
            os.remove(self.caminho)

    def textos(self, zip_saida, aba):
        raiz = ET.fromstring(zip_saida.read("xl/worksheets/sheet%d.xml" % aba))
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        return [no.text for no in raiz.iter(ns + "t")]

    def test_escreve_duas_abas_com_o_que_falta(self):
        excel.escrever(self.caminho, [
            ("Tarefas", ["TAREFA", "PENDENTE EM", "DIAS"],
             [["108692", "v2.2602: PR nao aberto", 32]], [10, 40, 6]),
            ("Pendencias por branch", list(ciclo.COLUNAS_LONGO),
             [["108692", "Corretiva", "Em teste", HOMO, "sim", "PR nao aberto",
               "sim", ciclo.SEM_VERSAO, "sim", "2602", "Erro ao copiar perfil"]], None),
        ])
        with zipfile.ZipFile(self.caminho) as zip_saida:
            self.assertIsNone(zip_saida.testzip())
            self.assertIn("xl/worksheets/sheet2.xml", zip_saida.namelist())
            self.assertIn("v2.2602: PR nao aberto", self.textos(zip_saida, 1))
            longo = self.textos(zip_saida, 2)
            self.assertIn(HOMO, longo)
            self.assertIn(ciclo.SEM_VERSAO, longo)

    def test_escapa_e_numero_continua_numero(self):
        excel.escrever(self.caminho, [("A", ["X & <Y>"], [["a<b", 7]], None)])
        with zipfile.ZipFile(self.caminho) as zip_saida:
            bruto = zip_saida.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("X &amp; &lt;Y&gt;", bruto)
        self.assertIn("<v>7</v>", bruto)

    def test_coluna_por_indice(self):
        self.assertEqual([excel.coluna(i) for i in (0, 1, 25, 26, 27)],
                         ["A", "B", "Z", "AA", "AB"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
