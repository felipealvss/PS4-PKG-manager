# PS4 PKG Manager

Baixa os PKGs do catálogo do FPKGi aqui no PC (multi-conexão, com retomada) e
entrega pro PS4 pela rede local.

    ps4pkg serve --open        # interface web em http://localhost:8420

A aba **Guia** dentro do programa tem tudo isto em forma de seções expansíveis,
com uma checagem ao vivo do ambiente no topo (console, disco, catálogo, rede) —
cada item diz o que está errado e o que fazer.

**[Diagnóstico do projeto](https://claude.ai/code/artifact/19fcbf89-f0f0-4288-8d2b-d7ef6c2db321)**
— o histórico completo: o que foi construído, as três conclusões que as medições
derrubaram pelo caminho e a telemetria real do download de 43,8 GB.
*(link privado — acessível apenas ao dono do artifact)*

Também tem lançador no menu de aplicativos ("PS4 PKG Manager").

## Por que isso ajuda

O FPKGi baixa com **uma conexão só**, de um servidor só. Medido nesta rede,
baixando o mesmo arquivo de 43,8 GB:

| | velocidade |
|---|---|
| PS4, 1 conexão (o que o FPKGi faz) | ~1,4 Mbps (0,18 MB/s) |
| PC, 16 conexões num espelho só | ~3 Mbps (0,38 MB/s) |
| **PC, 16 conexões em cada um dos 3 espelhos** | **204 Mbps (24,3 MB/s)** |
| PC → PS4 por FTP na LAN | 43 Mbps (5,4 MB/s) |

A diferença entre a segunda e a terceira linha é a coisa mais importante deste
programa. **O limite de banda do archive.org é por servidor, não por IP.** Um
único espelho congestionado entrega 0,38 MB/s e faz parecer que existe um teto;
os mesmos 43,8 GB, distribuídos entre os três espelhos do item, vieram a
24,3 MB/s. Um jogo de 43,8 GB sai de **31 horas para 23 minutos**.

Por isso o `resolve()` mede todos os espelhos antes de começar e usa todos.
Nunca confie num único número de velocidade do archive.org: o mesmo servidor
que dá 0,4 MB/s numa hora dá 27 MB/s em outra.

Com isso a etapa lenta passou a ser a **transferência para o console**: 43,8 GB
por FTP na LAN levam ~2h15. Ainda assim é tempo de rede local, sem prender o
console durante o download.

## Como funciona

O FPKGi não guarda a lista de jogos dentro do `.pkg` — com `populateViaWeb: true`
ele busca JSONs remotos em tempo de execução, e as URLs estão no `config.json`
do console. O gerenciador lê esse config por FTP (`/data/FPKGi/config.json`),
baixa os mesmos catálogos e trabalha em cima deles. Não precisa extrair o
`FPKGi_Data.pkg`.

## As três formas de levar pro PS4

1. **Catálogo local** (aba PS4 → "Apontar FPKGi para cá") — o PC serve um
   `GAMES.json` com os arquivos que você já baixou. O FPKGi passa a listar sua
   biblioteca local e instala na velocidade da LAN. O `config.json` original é
   salvo em backup antes, e o botão ao lado restaura.
   Exige o servidor rodando quando você usar o FPKGi.
2. **Transferência para o destino final** (aba Biblioteca → "Transferir") —
   manda o `.pkg` pra pasta que você escolher no console. Veja abaixo.
3. **USB** — pendrive em exFAT e o Package Installer do GoldHEN.

## Biblioteca unificada

As tabelas da **Fila** e da **Biblioteca** são ordenáveis por qualquer coluna:
clicar no cabeçalho alterna crescente, decrescente e de volta à ordem natural
(a ordem de execução, no caso da fila). Colunas numéricas começam do maior para
o menor, e a escolha persiste entre sessões. As três listas paginam de 8 em 8.

A aba Biblioteca traz duas listas empilhadas: os arquivos **no PC** e **o que já
está instalado no console**. A busca no topo filtra as duas simultaneamente, cada
uma pagina de 8 em 8 e o cabeçalho mostra a contagem com o tamanho total — a aba
tem altura previsível independente do tamanho da biblioteca.

A lista do console mostra — com o ícone real de cada título, o tamanho ocupado e de onde o pacote
veio, lido do próprio `app.json` do console: `local / USB`, `Duskaryon`,
`archive.org`. Também marca quais deles você tem em `.pkg` aqui, casando por
Title ID.

É estritamente somente leitura: nada nesse caminho escreve em `/user/app`. O
resultado fica em cache por 10 minutos, carrega só ao abrir a aba e, com o
console desligado, some com um aviso sem afetar o resto.

## Instalação direta (o caminho mais curto)

Na aba Biblioteca, **Instalar** faz o console baixar o pacote deste PC pela rede
local e instalar direto, sem cópia intermediária:

| | Transferir | **Instalar** |
|---|---|---|
| Espaço no console | ~88 GB (pkg + instalado) | **~44 GB** |
| Etapas | transferir, depois instalar | uma só |
| Cópia em `/data/pkg` | 43,8 GB | nenhuma |

Exige o **Package Installer aberto no console** (porta 12800). O programa detecta
e, quando não está respondendo, desabilita o botão e explica — a transferência
por FTP continua disponível como alternativa.

Duas coisas descobertas ao mapear a API do instalador, ambas necessárias para
funcionar:

- as respostas trazem números em **hexadecimal** (`0x1CC`), o que não é JSON
  válido — `json.loads()` sozinho falha;
- o cliente HTTP do console **decodifica a URL e não a recodifica** ao requisitar,
  então `Nidhogg - [US] [EN] [1.02].pkg` falha com *"Unable to set up
  prerequisites"*. Por isso os pacotes também são servidos sob um apelido ASCII
  simples em `/pkg/<hash>-<nome>.pkg`, usado só nas URLs entregues ao console.

O console resolve o nome do título sozinho a partir do `.pkg` e o devolve na
resposta, então a fila passa a mostrar "Nidhogg" no lugar do nome do arquivo.

## Transferência para o destino final

Escolha o destino no seletor da aba Biblioteca e clique em **Transferir**. O que
acontece por baixo:

- **Retoma de onde parou.** O FTP do GoldHEN 2.2 anuncia `REST STREAM`, então uma
  transferência interrompida continua do offset em que estava. Testado: 53 MB
  cancelados em 66% e retomados — arquivo final byte a byte idêntico ao local
  (sha256 conferido). Num `.pkg` de 40 GB isso é a diferença entre 10 minutos e
  recomeçar do zero.
- **Confere no fim.** Compara o tamanho no console (`SIZE`) com o local antes de
  marcar como concluído. Se divergir, o job falha em vez de mentir.
- **Não repete trabalho.** Se o arquivo já estiver completo lá, pula na hora.
- **Cria a pasta** no console se ainda não existir.
- **Apagar a cópia local** é opcional (caixa na aba Biblioteca ou nos Ajustes), e
  só acontece depois que o console confirmou o tamanho.

Os destinos são editáveis em Ajustes, um por linha no formato `Nome = /caminho`.
Vêm dois configurados:

    Pasta de PKG do console    = /data/pkg
    FPKGi - fila de downloads  = /data/FPKGi/Downloads

## Caminhos são variáveis, não constantes

Nada de caminho fixo no código. O firmware, o GoldHEN e o próprio FPKGi mudam de
lugar com o tempo, e não dá pra depender de editar fonte quando isso acontecer:

| ajuste | o que controla |
|---|---|
| `ps4_host`, `ps4_ftp_port` | onde o console está |
| `ps4_fpkgi_dir` | pasta do FPKGi; o `config.json` é derivado daqui |
| `ps4_destinations` | destinos de transferência (lista editável) |
| `ps4_default_destination` | qual deles vem selecionado |
| `dest` | pasta de download neste PC |
| `extra_sources` | catálogos extras, ou substitutos se o console sumir |

Se o caminho do FPKGi mudar e a leitura falhar, o programa **não quebra**: cai
para o `extra_sources` e segue funcionando sem o console.

### Por que não dá pra baixar direto pro FTP

O campo "Pasta de destino" recusa URLs. O motor de download grava com `pwrite`
em offsets arbitrários e pré-aloca o arquivo com `ftruncate` — é isso que
permite 16 conexões simultâneas e a retomada por pedaço. FTP não tem escrita em
offset arbitrário, e nem montando por FUSE isso funcionaria bem. O caminho certo
é baixar local e transferir, que é exatamente o que a aba Biblioteca faz.

Toda configuração é validada **antes** de ir pro disco: um valor inválido é
recusado com explicação e não fica gravado pela metade.

## Disponibilidade

Parte do acervo já foi bloqueada no archive.org: o metadata continua listando os
arquivos, mas o download responde 401/403. Hoje são **57 pacotes** (o item da
letra M). A interface marca esses em vermelho e desabilita o botão; a caixa
"só disponíveis" some com eles. O botão "Revalidar" refaz a checagem — vale
rodar de vez em quando, porque isso muda com o tempo.

## Retomada

Cada download é fatiado em pedaços de 4 MB gravados direto no offset certo do
arquivo. O estado (`.incomplete/<nome>.json`) guarda os pedaços prontos **e**
quanto já foi lido de cada pedaço em andamento — sem isso, cair no meio perderia
até 4 MB por conexão, o que dói a 50 KB/s por conexão.

Testado: 3 `kill -9` no meio de um download de 55 MB, retomado do ponto e
arquivo final íntegro. Todo `.pkg` é conferido pela assinatura `\x7fCNT` antes
de sair da pasta `.incomplete`.

## Linha de comando

    ps4pkg status                   # PS4, disco, catálogo
    ps4pkg search hollow knight     # busca
    ps4pkg get downwell             # enfileira e baixa
    ps4pkg queue --watch            # acompanha a fila
    ps4pkg library                  # o que já está no disco
    ps4pkg dests                    # destinos configurados no console
    ps4pkg push "jogo.pkg"                    # transfere pro destino padrão
    ps4pkg push "jogo.pkg" --dest /data/pkg   # ou pro que você escolher
    ps4pkg push "jogo.pkg" --delete-after     # apaga a cópia local depois
    ps4pkg refresh                  # rebaixa os catálogos
    ps4pkg link / ps4pkg unlink     # aponta o FPKGi pro PC / restaura

## Instalação

Não há dependências fora da biblioteca padrão do Python 3. O `ffmpeg` é opcional
e serve só para reduzir as capas.

    git clone git@github.com:felipealvss/PS4-PKG-manager.git
    cd PS4-PKG-manager
    ./ps4pkg.py serve --open

Na primeira execução, vá em **Ajustes** e informe o IP do seu PS4 e a pasta de
destino. A aba **Guia** tem uma checagem ao vivo que diz o que ainda falta.

Para ter o comando `ps4pkg` no PATH, de dentro da pasta do repositório:

    printf '#!/usr/bin/env bash\nexec python3 -u "%s/ps4pkg.py" "$@"\n' "$PWD" \
      > ~/.local/bin/ps4pkg && chmod +x ~/.local/bin/ps4pkg

## Onde ficam as coisas

    <pasta do repositório>/                    código
    <pasta de destino>/                        downloads (definida em Ajustes)
      └── .incomplete/                         parciais + estado de retomada
    ~/.local/share/ps4pkg/
      ├── settings.json    ajustes (a aba Ajustes escreve aqui)
      ├── catalog.json     cache dos catálogos (12h)
      ├── availability.json  o que o archive.org ainda serve (24h)
      ├── jobs.json        a fila (downloads e transferências), sobrevive a reinício
      ├── covers/          capas reduzidas com ffmpeg (443 KB → 38 KB)
      └── backups/         config.json do FPKGi antes de cada alteração

## Rede

O servidor escuta em `0.0.0.0:8420` porque o PS4 precisa alcançá-lo. Ou seja,
qualquer máquina da sua rede local vê a interface e os arquivos enquanto ele
estiver rodando. Pra restringir ao PC, mude `http_host` pra `127.0.0.1` no
`settings.json` — aí as opções 1 e 2 de envio pro PS4 param de funcionar.

Sem dependências fora da biblioteca padrão do Python. O `ffmpeg` é opcional, só
pra reduzir as capas.
