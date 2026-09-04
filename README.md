# Ferramentas de backport

Duas ferramentas de janela, para quem mantém **branches de versão** (produção, homologação)
separadas da branch principal e precisa levar correções de uma para as outras.

O problema que elas resolvem: você mandou duas correções para a `master`, portou só uma para
a branch de produção e descobriu a que faltou pelo cliente reclamando. E a variante pior: a
correção foi para a produção, mas ninguém levou para a versão que o cliente ia receber.

| Ferramenta | Para quê |
| ---------- | -------- |
| **BackportCheck** | **Descobrir** o que está na principal e ainda não chegou nas branches de versão — e em qual delas falta — e portar dali mesmo. |
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

## A explicação mora na célula

As duas janelas são escuras — inclusive a barra de título, que não é do Tk: quem desenha é o
Windows, e só obedece ao DWM (`DWMWA_USE_IMMERSIVE_DARK_MODE`). E **não existe bloco de
legenda**: pare o mouse em cima de uma
célula e o balão diz o que aquele valor quer dizer — a explicação da pendência, o que é
`não solicitado`, quais branches são obrigatórias naquela tarefa, o texto inteiro do que a
coluna cortou. No cabeçalho, o balão diz para que serve a coluna; no rótulo de um campo, o que
preencher. Legenda que ocupa um terço da janela e ninguém lê deixou de existir.

---

# BackportCheck

Responde "o que eu mandei para a principal e esqueci de mandar para a produção?".
Compara `origin/<principal>` com `origin/<produção>` e lista o que falta portar.

## Uso

| Campo | Exemplo | Observação |
| ----- | ------- | ---------- |
| Repositório | `C:\repos\meu-projeto` | qualquer repo git com remote `origin` |
| Branch de produção | `release-2601` | troque quando virar a versão |
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
| **PROVÁVEL** (cinza) | o número da tarefa já aparece na produção, com outro assunto | típico de PR de backport intitulado com o nome da branch |
| *(não listado)* | já portado | patch-id equivalente (`git log --cherry-pick`) ou assunto normalizado igual |

O número da tarefa é qualquer número de 5 a 7 dígitos no assunto do commit
(`Corretiva - tarefa 123456`). A normalização do assunto ignora acento, pontuação e o
`(#1234)` do squash merge — o PR do backport tem número diferente do PR original, então
ele não pode entrar na comparação.

O histórico de produção é lido com janela de 3 anos, independente do campo **Dias**:
um backport pode ter sido feito muito depois do commit original.

**PROVÁVEL não é prova.** Duas correções da mesma tarefa, com PRs diferentes, caem aí — e
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
  buscados pelo filtro `id` da API v3, de onde saem o tipo, o status, o campo de build e os dois
  campos que dizem em quais versões a tarefa tem de ser disponibilizada. Sem ele a aba funciona
  igual, deduzindo o tipo do título do PR. Campos personalizados são lidos tanto do corpo quanto
  de `_links` — listas e opções só aparecem lá.

| Situação | Significa | O que fazer |
| -------- | --------- | ----------- |
| **PODE MERGEAR** (verde) | a tarefa está num status de **concluída** e o PR continua aberto | mergear — é trabalho pronto parado |
| **SEM PR DE PRODUÇÃO** (vermelho) | a produção é obrigatória para essa tarefa e não há PR aberto para essa branch | abrir o backport (a aba git faz isso) |
| **FALTA EM OUTRA VERSÃO** (vinho) | falta numa **outra** branch obrigatória — homologação, ou uma versão pedida no card | abrir o backport daquela versão |
| **AGUARDA APROVAÇÃO** (azul) | existe PR aberto para uma branch de versão esperando revisão há N dias | cobrar revisão |
| **PARADO** (laranja) | PR aberto sem nenhuma atualização há mais dias que o limite | decidir: retomar ou fechar |
| **OK** (cinza) | nada a fazer pelo que dá para ver dos PRs abertos | — |

"Status de concluída" não é adivinhação pelo nome: vem do próprio gerenciador, que marca cada
status com `isClosed`. O botão **Status que liberam merge** serve para incluir também status
intermediários (um "teste aprovado", por exemplo) que a instância não considera fechados.

Os PRs são agrupados por **número da tarefa**, tirado do nome do branch (`fb_123456_2601`) ou
do título, então as duas pontas de uma mesma tarefa aparecem na mesma linha.

### Quais branches são obrigatórias

A pergunta "essa tarefa está pendente?" só tem resposta depois de "pendente **onde**?". Quem
decide isso é uma função só — `branches_obrigatorias()` em `ciclo.py` — e todo o resto (as
colunas, a pendência, o Excel) lê dela:

| Regra | Branches que entram |
| ----- | ------------------- |
| toda tarefa, sempre | a **principal** (master) |
| o tipo está marcado em **Tipos que exigem produção** | **produção** e **homologação** |
| o card tem **Confirmar entrega ao cliente?** marcado | as versões escritas em **ramos para disponibilização**, qualquer que seja o tipo |
| a tarefa **já está na produção** (pelo histórico do clone) | **homologação**, qualquer que seja o tipo — o que o cliente já recebeu não pode faltar na versão seguinte |

Versão que a tarefa **não** pediu não é cobrada: a coluna daquela branch mostra
`não solicitado` em vez de `PR não aberto`. É o que separa "falta portar" de "nunca foi para
ser portado" — o caso de um card que pede só a 2602 e não a 2601.

Nada aqui olha o *status* da tarefa para expandir branches: um card em "Desenvolvido" continua
cobrado só na principal (e na produção, se o tipo dele exigir) até que o campo de entrega ao
cliente diga o contrário.

### O campo "ramos para disponibilização"

O campo é texto livre e o preenchimento varia. O único padrão confiável são os **4 dígitos da
versão**, e é só isso que a ferramenta lê — o separador não importa:

| No card | Versões lidas |
| ------- | ------------- |
| `v2.2602` | 2602 |
| `2602` | 2602 |
| `2602 - 2601` | 2602, 2601 |
| `2602, 2601` / `2602/2601` / `v2.2602 - v2.2601` | 2602, 2601 |
| `próxima release` | nenhuma (não inventa branch) |

Cada versão é convertida para o nome de branch **deste** repositório, e o formato sai das
branches que você configurou: com produção `v2.2601`, o `2602` do card vira `v2.2602`; com
produção `release-2601`, vira `release-2602`. Não existe `v2.` fixo no código. A coluna
**RAMOS** mostra as versões já normalizadas — é a interpretação da ferramenta, dá para
conferir de olho.

### O que cada lado diz

As colunas **PR PRINCIPAL**, **PR PRODUÇÃO** e **PR HOMOLOGAÇÃO** não mostram só o número do
PR — mostram a situação daquele lado, que é o que interessa para agir:

| Texto | Significa |
| ----- | --------- |
| `mergeado` | a tarefa já aparece no histórico daquela branch (lido do clone local) |
| `aprovado, falta mergear` | PR aberto e já aprovado na revisão |
| `comentado, sem aprovar (Nd)` | alguém revisou e comentou, mas não apertou aprovar |
| `revisão pediu ajuste` | PR aberto com pedido de mudança |
| `aguardando aprovação (Nd)` | PR aberto e ninguém encostou ainda |
| `rascunho` | PR aberto como *draft* |
| `PR não aberto` | nada aberto e nada no histórico daquela branch |
| `sem PR aberto` | nada aberto e **não havia clone local** para conferir o histórico |
| `não solicitado` | aquela branch **não é obrigatória** para essa tarefa |

Para distinguir `mergeado` de `PR não aberto` é preciso ter o **clone local** informado no
campo Repositórios — é do histórico do git que sai essa resposta, não do GitHub. Sem clone,
os dois casos viram `sem PR aberto`, que é honesto sobre o que se sabe. É também de lá que sai
a resposta para "o commit já está na branch?": o histórico é lido **de cada branch obrigatória**,
inclusive as que vieram do campo de ramos.

### A coluna PENDENTE EM

É a coluna que responde a pergunta que importa. Ela lista, branch por branch, o que ainda
falta — e nada mais:

```
v2.2601: aguardando aprovação (19d) #8030, v2.2602: PR não aberto
```

Uma branch sai dessa lista quando a tarefa aparece no histórico dela (`mergeado`) ou quando ela
não é obrigatória para essa tarefa. Se sobrar só `v2.2602: PR não aberto`, a pendência da tarefa
é exclusivamente a 2602.

> Sem clone local a ferramenta não sabe o que já foi mergeado, então **toda** branch obrigatória
> aparece em PENDENTE EM. Informe a pasta do clone no campo Repositórios e a coluna passa a
> mostrar só o que realmente falta.

**Clicar na célula de PR abre aquele PR no navegador** (o cursor vira mãozinha em cima das
células que têm link). **BUILD (X5)** mostra o campo de build da tarefa.

> Se o seu time não usa o botão *Approve* do GitHub, `aprovado, falta mergear` não vai
> aparecer — o que você verá é `comentado, sem aprovar` contra `aguardando aprovação`, que
> ainda separa o PR que alguém olhou do PR em que ninguém encostou.

### Dois escopos: PR aberto ou o projeto inteiro

Quem decide é a URL do OpenProject:

| URL | Escopo | Consequência |
| --- | ------ | ------------ |
| `https://op.empresa.com.br` | **só PR aberto** | a lista sai dos PRs abertos. O que foi mergeado e teve o PR apagado sai do radar. |
| `https://op.empresa.com.br/projects/meu-time` | **o projeto** | a lista sai das *tarefas* do projeto, com PR aberto ou sem. |

O segundo escopo existe por causa de um caso concreto: o PR foi para a principal, foi mergeado
e o branch apagado. Não há PR aberto em lugar nenhum, então o radar de PR não tem o que ver — e
a tarefa continua faltando na produção e na homologação, sem ninguém avisar. Com o projeto na
URL, é o **histórico do git** que responde "o commit está nessa branch?", e a tarefa volta a ser
cobrada mesmo sem PR.

No modo projeto:

- entram as tarefas de **qualquer status** (a fechada é justamente a que interessa) mexidas
  dentro da janela do campo **Tarefas dos últimos (dias)**;
- uma tarefa sem PR aberto só aparece se tiver o que fazer: faltar numa branch obrigatória ou
  estar com o campo de build vazio;
- tarefa que falta numa versão **e também não está na principal** fica de fora: isso não é
  backport atrasado, é trabalho que não terminou. O log diz quantas foram, para o corte não
  ser silencioso;
- o **clone local é obrigatório** na prática — é dele que sai a resposta sobre o histórico.
  Sem clone, toda branch obrigatória aparece como pendente, e o log avisa.

### Os campos

Na ordem em que aparecem na tela:

| Campo | O que é |
| ----- | ------- |
| **Repositórios** | `org/repo` separados por vírgula **ou** o caminho de um clone — nesse caso o `org/repo` é descoberto pelo remote `origin`. Vazio usa o repositório da aba git. |
| **OpenProject** | URL da sua instância, ou de um **projeto** dela (`.../projects/meu-time`) — com projeto, a análise deixa de depender de PR aberto (veja acima). **Opcional**: sem ela a aba roda só com o GitHub e deduz o tipo do título do PR. |
| **Tarefas dos últimos (dias)** | janela das tarefas do projeto. Vale **só** no modo projeto; no outro é ignorado. |
| **Token da API** | token pessoal do OpenProject (*Minha conta → Tokens de acesso*), **nunca** a sua senha. O botão **Onde pegar o token** abre essa página. Guardado cifrado nesta máquina. |
| **Branch de produção** | contra qual branch o PR de produção é esperado (ex.: `release-2601`). Vazio, usa a da aba git. A comparação ignora maiúsculas. |
| **Branch de homologação** | a próxima versão (ex.: `release-2602`). **Vazio, a ferramenta se comporta exatamente como antes**: uma branch de versão só. |
| **Query salva (id)** | **opcional.** O número que aparece na URL do gerenciador como `?query_id=1234` — a visão já filtrada do seu time, para trazer também tarefas que não têm PR aberto. As tarefas dos PRs são buscadas pelo número, independente dela. |
| **Parado após (dias)** | quantos dias sem **nenhuma** atualização no PR para ele ser marcado como PARADO. |
| **Conta do GitHub** | qual credencial usar (veja abaixo). |

As duas branches de versão são campos — não há nome de versão escrito no código. Quando a
produção virar a 2602, você troca os dois campos e nada mais.

### Os botões

A aba tem três linhas, e a diferença entre elas é o que muda: **ação**, **regra** e **filtro**.
Regra muda a *pendência* de cada tarefa e refaz a conta; filtro só *esconde linha* do
resultado que já está carregado.

- **Carregar** — busca os PRs abertos e as tarefas, e monta a lista. É a ação principal da aba.
- **Abrir tarefa / PR** — abre no navegador a tarefa selecionada e os PRs dela. Duplo clique
  na linha faz o mesmo.
- **Exportar Excel...** — grava o que está na tela (ver abaixo).

Na linha **Regras:**

- **Tipos que exigem produção...** — marque aqui os tipos que precisam chegar nas branches de
  versão. Na primeira abertura já vêm marcados **corretiva** e **dívida técnica**: manutenção
  precisa chegar na versão que o cliente usa, funcionalidade nova não. Desmarcar tudo e
  confirmar vale como escolha — ninguém é cobrado por falta de PR de produção, e o log avisa.
  Isto **não** esconde linha nenhuma: quem esconde é o filtro **Tipo**.
- **Status que liberam merge...** — status intermediários que também contam como "pronto"
  (os fechados na instância já contam sozinhos).

Os dois botões de regra só listam valores depois da primeira carga — eles mostram o que
existe na sua base, não uma lista fixa.

Cada tipo aparece **uma vez**, com a grafia do gerenciador. O tipo chega escrito de duas
formas — como o gerenciador manda (`Corretiva`) ou deduzido do título do PR, em minúsculas
(`corretiva`) — e para a regra sempre foram o mesmo tipo, porque a comparação ignora acento e
caixa. Marcar um marca os dois.

### Filtros

Cinco botões na linha **Filtrar:**, todos com o **mesmo** comportamento: clicar abre uma caixa
de marcar, e **nada marcado = filtro desligado** — nunca "não mostra nada". Agem sobre o
resultado já carregado: trocar filtro não refaz consulta nenhuma. Nenhum deles vem marcado.

| Filtro | Opções |
| ------ | ------ |
| **Atribuída a** | de quem é a **tarefa** no OpenProject. É o "o que é meu": o suporte abre a tarefa, mas quem responde por ela é a pessoa atribuída. Sem OpenProject configurado a lista fica vazia. |
| **Autor do PR** | quem **abriu o PR** no GitHub (login). Atenção: tarefa cujo PR já foi mergeado e apagado **não tem autor**, então marcar alguém aqui esconde justamente o que o modo projeto existe para achar. |
| **Tipo** | os tipos que apareceram na carga. Não confunda com a regra *Tipos que exigem produção*: esta esconde linha, aquela decide quem é cobrado. |
| **Entrega** | `Sim`, `Não`, `(não informado)`. Só `Sim` deixa na tela a fila de backport do momento; `(não informado)` acha a tarefa cujo campo não veio preenchido. |
| **Versão pedida** | as versões que apareceram na carga, mais `(nenhuma)` para quem não pediu versão. Marcar 2602 responde "o que ainda falta para a 2602?". |

**Limpar filtros** desliga os cinco de uma vez, e fica apagado quando não há nada filtrando.

Cada botão mostra o próprio valor (`Atribuída a: Rafael Mello`), porque caixa de diálogo
esconde estado. E o rodapé conta o que o filtro fez: `37 de 912 tarefa(s) na tela, 2 filtro(s)
ativo(s)`. Quando um filtro esconde **tudo**, o rodapé fica vermelho e manda limpar — a tela
já disse `0 tarefa(s)` havendo 912, e não havia como desconfiar. A lista de versões vem do que apareceu na sua base, não de uma lista fixa. E o
filtro de versão olha o que o **card pediu**; para saber se aquela versão já recebeu a tarefa, a
resposta está na coluna da branch e em PENDENTE EM.

### Exportação para Excel

O botão **Exportar Excel...** grava um `.xlsx` com duas abas, ambas com filtro automático e
cabeçalho congelado:

- **Tarefas** — exatamente as colunas da tela, na mesma ordem, respeitando os filtros e a
  ordenação ativos no momento da exportação;
- **Pendências por branch** — uma linha por **tarefa × branch**, com `BRANCH`,
  `OBRIGATÓRIA`, `SITUAÇÃO` e `PENDENTE` em colunas próprias.

A segunda aba existe porque é ela que responde por filtro, e não por leitura de texto:
`BRANCH = v2.2602` + `PENDENTE = sim` é a lista do que falta naquela versão. Uma tarefa
`master OK / v2.2601 aguardando aprovação / v2.2602 sem PR` vira três linhas, e nenhuma
informação fica escondida dentro de uma célula.

O arquivo é escrito sem biblioteca externa (`excel.py`, ~100 linhas de zip + XML), para o
executável continuar sendo só o Python embutido.

### Qual conta do GitHub ele usa

Quem escolhe a conta é o **caminho `org/repo`** enviado ao `git credential fill` — é assim que
o git resolve máquinas com mais de uma conta (`credential.https://github.com/<org>.username`
no seu gitconfig). Por isso o campo Repositórios importa: um caminho de pasta no lugar de
`org/repo` faz o git cair no helper padrão e devolver a conta errada.

O seletor **Conta do GitHub** oferece:

- `(automática pelo repositório)` — o certo em quase todo caso: pergunta ao git usando o `org/repo`;
- `(padrão do git)` — sem caminho, o que a máquina responder por padrão;
- as contas encontradas no Gerenciador de Credenciais do Windows, para forçar uma delas.

A conta efetivamente usada aparece em verde ao lado do seletor e no log, a cada carga.

### Configuração

Quais tipos exigem produção e quais status liberam o merge são **escolhidos por você**, em dois
botões que listam os valores encontrados na sua própria base — nada de processo de empresa
nenhuma vem embutido no código. A comparação de branch ignora maiúsculas, porque a API do
GitHub costuma devolver o nome da branch em minúsculas onde o git local mostra em maiúsculas.

Os dois campos personalizados lidos do gerenciador são achados **pelo nome**, ignorando acento,
pontuação e caixa (`Confirmar entrega ao cliente?` casa com `confirmar entrega ao cliente`):

| Campo no card | Tipo | Efeito |
| ------------- | ---- | ------ |
| `Confirmar entrega ao cliente?` | booleano | marcado, liga as branches do campo abaixo |
| `Ramos para disponibilização` | texto | as versões (4 dígitos) que a tarefa tem de receber |

Se nenhuma tarefa trouxer o campo booleano, o log avisa — é o sintoma de campo renomeado na
instância. Sem os dois campos a aba funciona como antes, só com a regra de tipo.

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
| Repositório | `C:\repos\meu-projeto` |
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

Python 3.11 e PyInstaller 6.22. Gerar os dois é um comando — duplo clique em
`gerar-exes.bat`, ou:

```
python gerar_exes.py
```

Ele gera pelos `.spec` versionados, **prova pelo bytecode** que cada módulo embutido
corresponde ao `.py` do disco (data nova não é prova: build com cache ou fonte errado
editado dá exe de hoje com comportamento velho) e avisa se `dist/` ficou com alteração
não commitada — gerar não publica, os exes são versionados. Sai com código 1 se algo não
confere. Para só conferir os exes que já existem, sem gerar:

```
python gerar_exes.py --verificar
```

Feche os executáveis antes: o PyInstaller não sobrescreve exe aberto (o script checa e
avisa). Se preferir na mão, sem o script:

```
python -m PyInstaller --noconfirm BackportCheck.spec
```

```
python -m PyInstaller --noconfirm CherryPickPush.spec
```

`pyinstaller` solto pode não estar no PATH quando instalado com `pip install --user`;
`python -m PyInstaller` funciona nos dois casos.

`backport_check.py` importa o núcleo git de `cherrypick_tool.py` e mais `ciclo.py`,
`github_prs.py`, `openproject.py` e `excel.py`; todos precisam estar na mesma pasta. Nenhuma
dependência externa — o PyInstaller acha os módulos sozinho. Os últimos valores digitados ficam
em `%APPDATA%\cherrypick-tool\` (`backport.json` e `last.json`).

## Testes

A regra do ciclo (branches obrigatórias, leitura do campo de ramos, situação por branch,
formato longo e o escritor de xlsx) é testada sem nada instalado além do Python:

```
python -m unittest -v
```

Boa parte dos testes existe para travar o comportamento **antigo** — uma branch de versão só,
sem os campos do gerenciador. Se alguma alteração futura mudar o que a ferramenta já respondia,
esses testes quebram primeiro.

## Licença

MIT — veja [LICENSE](LICENSE).
