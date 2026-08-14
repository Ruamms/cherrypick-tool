# Ferramentas de backport

Duas ferramentas de janela, para quem mantém uma **branch de produção** separada da branch
principal e precisa levar correções de uma para a outra.

O problema que elas resolvem: você mandou duas correções para a `master`, portou só uma para
a branch de produção e descobriu a que faltou pelo cliente reclamando.

| Ferramenta | Para quê |
| ---------- | -------- |
| **BackportCheck** | **Descobrir** o que está na principal e ainda não chegou na produção — e portar dali mesmo. |
| **CherryPickPush** | **Portar** quando você já sabe o commit e o nome da branch. |

O trabalho com git é puro `git` — sem API e sem servidor. Só a aba **Ciclo** do BackportCheck
fala com serviços externos (GitHub e, opcionalmente, OpenProject), e ela é somente leitura.

## Baixar

Não precisa de Python nem saber compilar — baixe o `.exe` e execute:

- **[BackportCheck.exe](../../raw/main/dist/BackportCheck.exe)** (~11 MB)
- **[CherryPickPush.exe](../../raw/main/dist/CherryPickPush.exe)** (~11 MB)

> Os executáveis não são assinados digitalmente. Na primeira execução o Windows mostra
> *"O Windows protegeu o seu PC"* — clique em **Mais informações → Executar assim mesmo**.
> Se preferir não confiar num binário, o código-fonte são dois arquivos `.py` nesta pasta;
> a seção [Build](#build) mostra como gerar o exe você mesmo.

## Pré-requisitos

- Windows 64-bit;
- `git` no PATH (`git --version` tem que responder). A coluna **CONFLITO** usa
  `git merge-tree --write-tree`, que existe a partir do **git 2.38**; em versões mais
  antigas a coluna mostra `?` e o resto funciona normalmente;
- um clone do repositório, com remote `origin` e o working tree limpo na hora de portar.

O executável embute o runtime do Python — **não** é preciso ter Python instalado.

Nas duas abas, **clicar no cabeçalho de uma coluna ordena por ela**; clicar de novo inverte
(▲/▼ marcam a coluna ativa). A ordenação é sobre o resultado já carregado — não refaz consulta.

---

# BackportCheck

Responde "o que eu mandei para a principal e esqueci de mandar para a produção?".
Compara `origin/<principal>` com `origin/<produção>` e lista o que falta portar.

## Uso

| Campo | Exemplo | Observação |
| ----- | ------- | ---------- |
| Repositorio | `C:\repos\meu-projeto` | qualquer repo git com remote `origin` |
| Branch de producao | `release-2601` | troque quando virar a versão |
| Branch principal | `master` | ou `main` |
| Autor | `(todos)` | lista preenchida pela análise; escolha o seu nome para ver só o seu |
| Dias | `180` | janela de commits da principal |

Clique em **Analisar**. Trocar o autor depois **não** reprocessa o git — o filtro é aplicado
sobre o resultado já em memória.

Tudo fica salvo em `%APPDATA%\cherrypick-tool\backport.json`.

## As quatro situações

| Situação | Significa | Detectado por |
| -------- | --------- | ------------- |
| **PENDENTE** (vermelho) | nenhum sinal de backport | é o caso que queima cliente |
| **BRANCH CRIADA** (laranja) | a branch de backport existe no `origin`, mas nada chegou na produção | PR de backport aberto ou abandonado |
| **PROVAVEL** (cinza) | o número da tarefa já aparece na produção, com outro assunto | típico de PR de backport intitulado com o nome da branch |
| *(não listado)* | já portado | patch-id equivalente (`git log --cherry-pick`) ou assunto normalizado igual |

O número da tarefa é qualquer número de 5 a 7 dígitos no assunto do commit
(`Corretiva - tarefa 123456`). A normalização do assunto ignora acento, pontuação e o
`(#1234)` do squash merge — o PR do backport tem número diferente do PR original, então
ele não pode entrar na comparação.

O histórico de produção é lido com janela de 3 anos, independente do campo **Dias**:
um backport pode ter sido feito muito depois do commit original.

**PROVAVEL não é prova.** Duas correções da mesma tarefa, com PRs diferentes, caem aí — e
só uma pode ter ido. Confira antes de descartar.

## Coluna CONFLITO

Cada linha é testada com um cherry-pick **simulado** sobre a ponta da produção:

| Valor | Significa |
| ----- | --------- |
| `limpo` | aplica sem conflito — dá para portar agora |
| `conflito (N arq.)` | vai parar para você resolver na mão; selecione a linha e a barra de status mostra quais arquivos |
| `...` | ainda checando |
| `?` | não deu para simular (commit raiz, por exemplo) |

A simulação usa `git merge-tree`, que faz o merge de três vias **em memória**: nada é
alterado no seu working tree, no índice ou em qualquer branch. Roda em segundo plano,
preenchendo linha a linha, sem travar a janela.

A checagem é de cada commit **isolado** contra a produção. Ao levar vários commits de uma
vez, o segundo é aplicado depois do primeiro, então o resultado real pode ser melhor que o
mostrado.

## Cherry-pick + push pelo botão

Selecione uma linha (ou **várias**, para levar tudo na mesma branch — o caso de uma tarefa
com dois PRs na principal) e clique em **Cherry-pick + push do selecionado**:

1. sugere o nome da branch, editável na hora: `fb_<tarefa>_<sufixo>`, sufixo = último grupo
   de dígitos da branch de produção (`release-2601` → `fb_123456_2601`);
2. avisa se algum commit selecionado não é PENDENTE, ou se algum vai dar conflito;
3. cria a branch a partir de `origin/<produção>`, faz cherry-pick na ordem cronológica,
   dá push e abre a página de criação do PR no navegador;
4. re-analisa a lista no fim.

Em conflito ele **para** e não empurra nada — mesmas travas do CherryPickPush.

> Se a sua branch principal usa squash merge, o nome da branch de origem do PR não existe
> no histórico: sobram o assunto e o `(#1234)`. Por isso o padrão é `fb_<tarefa>_<sufixo>`,
> e não `<branch origem>_<sufixo>`.

O botão **Abrir PR de origem** — ou duplo clique na linha — abre no navegador o PR que originou
aquele commit (ou o próprio commit, se o assunto não trouxer o `(#1234)`).

## Aba "Ciclo": PRs abertos + OpenProject

A aba git responde "o que **está** onde". A aba Ciclo responde "o que **deveria** estar" —
porque a regra (que tipo de tarefa precisa chegar na produção) e o estado do teste moram no
gerenciador de tarefas, não no git.

Ela cruza duas fontes e não depende do `gh` CLI:

- **GitHub** — os PRs **abertos** dos repositórios que você listar. A credencial é pedida ao
  próprio git (`git credential fill`), ou seja, a mesma que o seu `git push` já usa: nada é
  digitado, nada é guardado por esta ferramenta.
- **OpenProject** (opcional) — os work packages **dos números de tarefa encontrados nos PRs**,
  buscados pelo filtro `id` da API v3, de onde saem o tipo, o status e o campo de build. Sem ele
  a aba funciona igual, deduzindo o tipo do título do PR. Campos personalizados são lidos tanto
  do corpo quanto de `_links` — listas e opções só aparecem lá.

| Situação | Significa | O que fazer |
| -------- | --------- | ----------- |
| **PODE MERGEAR** (verde) | a tarefa está num status de **concluída** e o PR continua aberto | mergear — é trabalho pronto parado |
| **SEM PR DE PRODUCAO** (vermelho) | o tipo exige produção e não há PR aberto para essa branch | abrir o backport (a aba git faz isso) |
| **AGUARDA APROVACAO** (azul) | existe PR aberto para a produção esperando revisão há N dias | cobrar revisão |
| **FALTA A BUILD (X5)** (âmbar) | tarefa concluída, sem PR aberto, e o campo de build vazio | preencher a versão no card |
| **PARADO** (laranja) | PR aberto sem nenhuma atualização há mais dias que o limite | decidir: retomar ou fechar |
| **OK** (cinza) | nada a fazer pelo que dá para ver dos PRs abertos | — |

"Status de concluída" não é adivinhação pelo nome: vem do próprio gerenciador, que marca cada
status com `isClosed`. O botão **Status que liberam merge** serve para incluir também status
intermediários (um "teste aprovado", por exemplo) que a instância não considera fechados.

Os PRs são agrupados por **número da tarefa**, tirado do nome do branch (`fb_123456_2601`) ou
do título, então as duas pontas de uma mesma tarefa aparecem na mesma linha.

### O que cada lado diz

As colunas **PR PRINCIPAL** e **PR PRODUCAO** não mostram só o número do PR — mostram a
situação daquele lado, que é o que interessa para agir:

| Texto | Significa |
| ----- | --------- |
| `mergeado` | a tarefa já aparece no histórico daquela branch (lido do clone local) |
| `aprovado, falta mergear` | PR aberto e já aprovado na revisão |
| `comentado, sem aprovar (Nd)` | alguém revisou e comentou, mas não apertou aprovar |
| `revisao pediu ajuste` | PR aberto com pedido de mudança |
| `aguardando aprovacao (Nd)` | PR aberto e ninguém encostou ainda |
| `rascunho` | PR aberto como *draft* |
| `PR nao aberto` | nada aberto e nada no histórico daquela branch |
| `sem PR aberto` | nada aberto e **não havia clone local** para conferir o histórico |

Para distinguir `mergeado` de `PR nao aberto` é preciso ter o **clone local** informado no
campo Repositórios — é do histórico do git que sai essa resposta, não do GitHub. Sem clone,
os dois casos viram `sem PR aberto`, que é honesto sobre o que se sabe.

**Clicar na célula de PR abre aquele PR no navegador** (o cursor vira mãozinha em cima das
células que têm link). **BUILD (X5)** mostra o campo de build da tarefa.

> Se o seu time não usa o botão *Approve* do GitHub, `aprovado, falta mergear` não vai
> aparecer — o que você verá é `comentado, sem aprovar` contra `aguardando aprovacao`, que
> ainda separa o PR que alguém olhou do PR em que ninguém encostou.

**Escopo desta versão: só PR aberto.** O que já foi mergeado sai do radar. Por isso a pendência
se chama *sem PR aberto para produção* e não *não está na produção* — para saber isso, use a
aba git, que lê o histórico e não depende de PR nenhum.

### Os campos

Na ordem em que aparecem na tela:

| Campo | O que é |
| ----- | ------- |
| **Repositorios** | `org/repo` separados por vírgula **ou** o caminho de um clone — nesse caso o `org/repo` é descoberto pelo remote `origin`. Vazio usa o repositório da aba git. |
| **OpenProject** | URL da sua instância. **Opcional**: sem ela a aba roda só com o GitHub e deduz o tipo do título do PR. |
| **Token da API** | token pessoal do OpenProject (*Minha conta → Tokens de acesso*), **nunca** a sua senha. O botão **Onde pegar o token** abre essa página. Guardado cifrado nesta máquina. |
| **Autor** | filtra por quem abriu o PR; a lista é preenchida pela carga. `(todos)` mostra o time inteiro. |
| **Branch de producao** | contra qual branch o PR de produção é esperado (ex.: `release-2601`). Vazio, usa a da aba git. A comparação ignora maiúsculas. |
| **Query salva (id)** | **opcional.** O número que aparece na URL do gerenciador como `?query_id=1234` — a visão já filtrada do seu time, para trazer também tarefas que não têm PR aberto (é o que permite pegar o caso *FALTA A BUILD*). As tarefas dos PRs são buscadas pelo número, independente dela. |
| **Parado apos (dias)** | quantos dias sem **nenhuma** atualização no PR para ele ser marcado como PARADO. |
| **Conta do GitHub** | qual credencial usar (veja abaixo). |

### Os botões

- **Carregar** — busca os PRs abertos e as tarefas, e monta a lista. É a ação principal da aba.
- **Tipos que exigem producao...** — marque aqui os tipos (corretiva, dívida técnica, o que for
  na sua casa) que precisam chegar na produção. **Sem nenhum marcado, ninguém é cobrado por
  falta de PR de produção** — o log avisa quando isso acontece.
- **Status que liberam merge...** — status intermediários que também contam como "pronto"
  (os fechados na instância já contam sozinhos).
- **Abrir tarefa / PR** — abre no navegador a tarefa selecionada e os PRs dela. Duplo clique
  na linha faz o mesmo.

Os dois botões de configuração só listam valores depois da primeira carga — eles mostram o que
existe na sua base, não uma lista fixa.

### Qual conta do GitHub ele usa

Quem escolhe a conta é o **caminho `org/repo`** enviado ao `git credential fill` — é assim que
o git resolve máquinas com mais de uma conta (`credential.https://github.com/<org>.username`
no seu gitconfig). Por isso o campo Repositórios importa: um caminho de pasta no lugar de
`org/repo` faz o git cair no helper padrão e devolver a conta errada.

O seletor **Conta do GitHub** oferece:

- `(automatica pelo repositorio)` — o certo em quase todo caso: pergunta ao git usando o `org/repo`;
- `(padrao do git)` — sem caminho, o que a máquina responder por padrão;
- as contas encontradas no Gerenciador de Credenciais do Windows, para forçar uma delas.

A conta efetivamente usada aparece em verde ao lado do seletor e no log, a cada carga.

### Configuração

Quais tipos exigem produção e quais status liberam o merge são **escolhidos por você**, em dois
botões que listam os valores encontrados na sua própria base — nada de processo de empresa
nenhuma vem embutido no código. A comparação de branch ignora maiúsculas, porque a API do
GitHub costuma devolver o nome da branch em minúsculas onde o git local mostra em maiúsculas.

O botão **Onde pegar o token** abre a página de tokens da sua própria instância
(`/my/access_token`) no navegador — preencha a URL antes de clicar. Você cola o token **uma
vez**: ele volta preenchido nas próximas aberturas, e é gravado também ao fechar a janela.

O token do OpenProject (gerado em *Minha conta → Tokens de acesso*; **nunca** a sua senha) fica
em `%APPDATA%\cherrypick-tool\backport.json`, que está fora da pasta do repositório — não há
como subir por engano — e é cifrado com a **DPAPI do Windows**, amarrada à sua conta: copiar o
arquivo para outra máquina não serve de nada.

---

# CherryPickPush

Cria a branch final a partir de `origin/<branch base>`, faz cherry-pick dos commits
informados e publica sob confirmação.

## Uso

Preencha os 4 campos e clique em **Preparar**:

| Campo | Exemplo |
| ----- | ------- |
| Repositorio | `C:\repos\meu-projeto` |
| Branch base | `release-2601` |
| Commits | `9f8e7d6c5b4a39281706f5e4d3c2b1a098765432` |
| Branch final | `fb_123456_2601` |

Vários commits: um por linha, aplicados na ordem digitada.

O botão **Push** só habilita depois de um cherry-pick limpo, e revalida o estado antes de
enviar. Se o push der certo e o checkbox **Abrir a página do PR no navegador após o push**
estiver marcado (padrão), abre `<origin>/compare/<base>...<final>?expand=1` no navegador —
só a página de criação; o PR nasce quando você confirmar lá. A URL também vai para o log.

Botão **Abortar cherry-pick** = `git cherry-pick --abort` (não apaga a branch criada).

## O que ele faz

```
git fetch origin --prune
git checkout --no-track -b <final> origin/<base>
git cherry-pick <commit>...
git push -u origin <final>        # só no clique do botão Push
```

A URL web vem de `git remote get-url origin` — aceita `https://` e `git@host:org/repo`.

---

## Travas

- Aborta se o working tree estiver sujo.
- Aborta se já houver cherry-pick/merge em andamento.
- Aborta se a branch final já existir local ou no `origin`.
- Aborta se a base ou algum commit não existir depois do fetch.
- Em conflito: **para**, lista os arquivos e deixa o estado para resolver na mão. Nunca faz
  `--continue` sozinho, nunca empurra.
- `--no-track` na criação da branch, para o upstream não nascer apontando para a base.

## O que elas não fazem

- Não usam a API do GitHub; não pedem, não guardam e não leem token nenhum. O push sai pelo
  seu `git`, com as suas credenciais — o PR aparece no seu nome.
- Não fazem `push --force`, não apagam branch e não reescrevem histórico.
- Não alteram nada no repositório para checar conflito.
- Não criam o PR sozinhas: abrem a página de criação e param aí.

## Build

Python 3.11 e PyInstaller 6.22:

```
python -m PyInstaller --noconfirm --onefile --noconsole --name BackportCheck backport_check.py
```

```
python -m PyInstaller --noconfirm --onefile --noconsole --name CherryPickPush cherrypick_tool.py
```

`backport_check.py` importa o núcleo git de `cherrypick_tool.py`; os dois arquivos precisam
estar na mesma pasta. Os últimos valores digitados ficam em `%APPDATA%\cherrypick-tool\`
(`backport.json` e `last.json`).

## Licença

MIT — veja [LICENSE](LICENSE).
