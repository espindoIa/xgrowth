# xgrowth — perfil no X no piloto automático, com testes que se ajustam sozinhos

Um robô que escreve os posts (com o Gemini, do Google), escolhe os horários, publica, mede o resultado de cada post 24h depois e usa isso para decidir o próximo: quais temas, formatos, tons, aberturas e horários dão mais views, respostas, compartilhamentos e cliques no perfil. Ele também responde quem interage com o perfil, dentro das regras do X.

Tudo roda sozinho a cada 30 minutos no GitHub Actions (grátis) ou no seu PC. Você só define o nicho e a voz uma vez.

## O que ele faz

O ciclo é: **planejar → escrever → publicar → medir → aprender → repetir**.

Todo dia o robô escolhe os horários (4 a 5 por dia, conforme o config, entre 7h e 23h, com 2h de intervalo). Uma hora antes de cada um, escolhe a combinação de pilar de conteúdo × formato × tom × gancho de abertura e pede o post ao Gemini, passando os posts recentes (para não se repetir) e os que mais deram certo (para aprender o estilo que funciona). Cada rascunho passa por uma checagem automática antes de sair: limite de caracteres, sem links, sem @menções, no máximo 1 hashtag, sem "dá like/me siga" e sem ser parecido demais com algo recente. Se falhar, volta para o Gemini com o motivo.

Depois de publicado, o robô coleta views, curtidas, respostas, reposts, citações, salvamentos e cliques no perfil. Com 24h, o post vira um dado para o otimizador.

Respostas: quando alguém responde a um post ou menciona o perfil, o Gemini escreve uma resposta curta e específica (ou decide não responder spam, provocação ou assunto sensível). Por padrão ela chega no seu Telegram com botões ✅/❌ e sai com um toque. Quem escreve "pare"/"stop" nunca mais recebe resposta automática.

Todo dia de manhã chega um resumo no Telegram, e o arquivo `data/relatorio.md` mostra o que o otimizador aprendeu (melhores horários, formatos, pilares, tons, ganchos e posts).

## O que ele não faz, de propósito

Nada disso é limitação técnica: é o que faz uma conta ser suspensa. Pelas [diretrizes de desenvolvedor do X](https://docs.x.com/developer-guidelines), contas automatizadas não podem curtir, seguir ou dar follow-back automaticamente, responder a quem não interagiu primeiro (por palavra-chave, por exemplo), postar em assuntos do momento para ganhar visibilidade, usar várias contas com o mesmo conteúdo ou automatizar pelo navegador em vez da API. E **respostas geradas por IA exigem aprovação prévia do X**, mesmo quando são úteis. Por isso as respostas começam no modo "aprovar".

Também evitamos o que o próprio algoritmo pune: o código aberto do ranking do X ([xai-org/x-algorithm](https://github.com/xai-org/x-algorithm)) mostra que bloqueios, "não tenho interesse" e denúncias derrubam a visibilidade da conta, então o prompt proíbe polêmica gratuita e ataques. Links ficam de fora porque custam 13× mais na API e costumam reduzir o alcance.

## Perfil de torcedor do Cruzeiro (o `config.yaml` deste repositório)

O `config.yaml` já vem preenchido para uma conta de torcedor do Cruzeiro **opinativa e provocativa**: seis pilares (hot take, cobrança da Nação, provocação de rival, verdade incômoda, dia de jogo e debate da Nação), formatos que forçam a torcida a tomar lado (debate, enquete A ou B, zoeira, lista de verdades, thread argumentada) e três tons (provocativo, debochado, categórico). Todo post precisa de posição clara, sem "depende", mas também de um argumento ou de uma sacada. Troque os `exemplos_de_posts` pelos seus assim que puder.

A provocação mira **ideias, atitudes e a rivalidade entre clubes, nunca pessoas pelo que elas são**, e isso é uma regra de sobrevivência da conta, não só de educação: bloqueios, "não tenho interesse" e denúncias derrubam o alcance do perfil inteiro, e ataque a origem, cor, orientação ou aparência pode suspender a conta. Um limite que o robô não enxerga: a API não informa quantos bloqueios e denúncias cada post gerou. Como respostas e citações valem muito na pontuação, o otimizador pode passar a preferir posts que irritam em vez de posts que convencem. Fique de olho no relatório e, se a conta perder seguidores enquanto as views sobem, suavize o tom ou use `posts.modo: aprovar` por uns dias.

**Crítica ao time e à diretoria.** O pilar `cobranca_ao_clube` cobra o time, a comissão técnica e os dirigentes, e também reconhece quando merecem. Para o robô saber *quando* é necessário, ele só entra em campo se existir um fato recente que sustente a crítica; sem fato, ele nunca critica nem elogia o clube atual (inventar uma crise inexistente seria o pior erro possível). Você alimenta isso pelo Telegram, do celular: mande `/fato O Cruzeiro perdeu em casa e o técnico manteve o mesmo time.` e o fato passa a valer nos próximos rascunhos (a leitura acontece a cada ciclo de 30 minutos e o fato vale por 3 dias; `/fatos` lista o que está em uso e `/esquecer` apaga). Só o chat configurado em `TELEGRAM_CHAT_ID` pode enviar fatos. Cuide para só enviar o que for verdade: o robô trata o fato como certo e constrói a opinião em cima dele. A crítica é sempre à decisão, à atuação ou à gestão, nunca à pessoa, sem acusar ninguém de crime, corrupção ou má-fé.

O maior risco de uma conta de futebol automatizada é o modelo **inventar fato**: placar, ano de título, escalação, contratação. Por isso há três travas. O Gemini só pode afirmar o que estiver em `contexto.fatos_atuais`. Qualquer rascunho que cite um placar (como "3x1") ou um ano passado (como "1997") que não esteja nesses fatos é rejeitado e reescrito. E o gancho "abrir com um número" ficou de fora do catálogo. Sem fatos informados, o robô fala de emoção, opinião e cenários de torcedor, que é onde ele não erra. Para comentar o jogo da rodada, escreva um ou dois fatos verdadeiros em `fatos_atuais` (dá para editar o arquivo pelo app do GitHub).

Em `contexto.dias_de_jogo` você lista as datas dos jogos (AAAA-MM-DD). Dia de jogo muda o tamanho do público, e sem essa informação o otimizador confundiria o efeito do jogo com o do horário ou do formato. Numa simulação em que o jogo triplicava as views e os horários não importavam, o modelo sem o calendário "descobriu" horários bons que não existiam; com o calendário, os efeitos de horário caíram para praticamente zero (dispersão de 0,03 contra 0,65). A bio deve dizer que a conta é de torcedor, **não oficial**, e automatizada.

## Passo a passo

**1. Crie a conta no X (manual).** Nome, foto, capa e uma bio que diga que é automatizada, por exemplo: *"Torcedor do Cruzeiro · conta não oficial e automatizada · gerenciada por @seuperfil"*. Depois ative o rótulo de conta automatizada nas configurações da conta (seção de informações da conta → Automação) e vincule ao seu perfil pessoal, que é obrigatório.

**2. Crie o acesso à API.** Entre em [console.x.com](https://console.x.com) **logado na conta do robô** (assim a leitura das próprias métricas custa US$ 0,001 por post em vez de US$ 0,005). Crie um app, em *User authentication settings* marque **Read and Write**, e só então gere *API Key/Secret* e *Access Token/Secret* (tokens gerados antes da permissão ficam só leitura). Não existe plano gratuito da API do X: ela é paga por uso, sem mensalidade (ao salvar o primeiro cartão, o X dá US$ 20 em créditos). Compre créditos e defina um limite de gasto.

**3. Chave do Gemini.** Crie em [aistudio.google.com/apikey](https://aistudio.google.com/apikey). Os modelos Flash têm camada gratuita, que para este uso (poucas chamadas por dia) costuma bastar. Na camada gratuita o Google pode usar o conteúdo enviado para melhorar os produtos dele; se isso incomodar (por exemplo, com respostas ligadas, que enviam o texto de quem te menciona), ative o faturamento no projeto do AI Studio.

**4. Telegram (opcional, recomendado).** No Telegram, fale com o @BotFather, use `/newbot` e guarde o token. Mande `/start` para o seu bot novo e rode `python -m xgrowth telegram-id` para descobrir o `TELEGRAM_CHAT_ID`. O robô só aceita cliques vindos desse chat.

**5. Configure.** O `config.yaml` do Cruzeiro já está pronto; para outro nicho, copie `config.example.yaml` e edite nicho, público, voz e pilares. Colar de 3 a 10 posts de exemplo da voz que você quer em `exemplos_de_posts` faz muita diferença. Copie `.env.example` para `.env` e preencha as chaves.

**6. Teste sem publicar.** Com `seguranca.dry_run: true` (padrão):

```bash
pip install -r requirements.txt
python -m xgrowth verificar   # confere config, chaves e manda uma mensagem de teste no Telegram
python -m xgrowth tick        # planeja o dia e mostra os posts que seriam publicados
python -m xgrowth plano
```

**7. Ponha no ar com o GitHub Actions.** Crie um repositório **privado**, suba esta pasta (com o `config.yaml`, sem o `.env`) e cadastre as chaves em *Settings → Secrets and variables → Actions* com os mesmos nomes do `.env`. Mude `dry_run` para `false`, faça o commit e, em *Actions*, clique em *Run workflow* para o primeiro ciclo. Daí em diante ele roda a cada 30 minutos. O estado e o relatório ficam no branch `estado` (abra `data/relatorio.md` por lá, inclusive pelo app do GitHub).

Prefere o seu PC? `python -m xgrowth daemon --intervalo 10` com `agenda.intervalo_ciclo_min: 10` no config. Só funciona enquanto o PC estiver ligado.

**8. Peça a liberação das respostas por IA.** Preencha o [formulário de suporte de plataforma do X](https://help.x.com/forms/platform) descrevendo o uso: respostas só a quem interagiu primeiro, uma por interação, com opt-out. Quando aprovarem, mude `respostas.modo` para `auto` e `tenho_aprovacao_do_x_para_respostas_ia` para `true`.

## Como a otimização funciona

Cada post é uma combinação de cinco escolhas: pilar, formato (no config do Cruzeiro: opinião curta, pergunta, enquete A ou B, cenário, lista, zoeira, thread), tom, gancho de abertura e horário. Depois de 24h, ele ganha uma pontuação: `log(views) + log(engajamento ponderado)`, em que resposta vale 3, repost e citação 4, salvamento 2, curtida 1 e clique no perfil 5 (é o melhor indicador de novo seguidor). O log impede que um post viral isolado distorça tudo.

Uma regressão bayesiana estima quanto cada escolha soma ou tira da pontuação, descontando o efeito de a conta estar crescendo e de ser fim de semana. Em vez de um teste A/B que divide o tráfego igualmente até o fim, o robô usa **Thompson Sampling**: sorteia uma hipótese plausível a partir do que já sabe e age como se ela fosse verdade. No começo, quando tudo é incerto, isso testa de tudo; conforme os dados chegam, ele concentra os posts no que funciona sem parar de testar o resto. Dados antigos perdem peso (meia-vida de ~23 dias), e se os posts recentes começarem a contrariar o que o modelo sabia, a memória encurta sozinha para se readaptar mais rápido.

### Calibragem por simulação

Antes de gastar um centavo de API, testei o otimizador contra públicos sintéticos com efeitos escondidos e ruído pesado (o mesmo post pode ter o dobro ou a metade das views por acaso). Medida: quanto do ganho máximo possível cada estratégia captura (0% = sortear tudo, 100% = um oráculo que conhece o público). 32 públicos diferentes, 90 dias, 4 posts por dia, resultado dos dias 61–90:

| cenário | horários fixos (9h, 12h, 18h, 21h) | 1ª versão do otimizador | versão final |
|---|---|---|---|
| público estável | 2% | 55% | **81%** |
| público muda de hábito no dia 45 | 6% | 34% | **51%** |
| ruído muito alto | 2% | 45% | **70%** |
| efeitos combinados que o modelo não vê | 1% | 59% | **87%** |

Nos primeiros 30 dias a versão final já captura cerca de 50%. O que mudou da 1ª para a final: menos exploração forçada (15% → 3%), amostragem mais decidida (escala 1,0 → 0,35) e memória adaptativa. Rode `python -m xgrowth simular` para reproduzir.

Simulação não é o mundo real: ela mostra que o otimizador encontra o que funciona rápido, mas o teto depende do nicho e da qualidade do conteúdo.

## Custos (outubro de 2026)

**API do X:** não há plano gratuito; é pago por uso ([preços oficiais](https://docs.x.com/x-api/getting-started/pricing)): US$ 0,015 por post ou resposta, US$ 0,20 se tiver link (por isso o robô não usa links), US$ 0,001 por post lido da própria conta. Cinco posts por dia custam cerca de US$ 2,25 por mês; com as respostas ligadas e as leituras, o total fica perto de **US$ 8 a 10 por mês**, e o X dá US$ 20 em créditos ao salvar o primeiro cartão. É o único custo obrigatório.

**Gemini:** o `gemini-3.8-flash` (padrão) e o `gemini-3.5-flash-lite` (mais barato) têm camada gratuita; fora dela, o preço por milhão de tokens está na [página oficial](https://ai.google.dev/gemini-api/docs/pricing). Com algumas chamadas curtas por dia, o gasto é de centavos. O nível de raciocínio (`llm.raciocinio`) vem em "low" para economizar.

**GitHub Actions:** cerca de 1.440 minutos por mês. Repositório privado tem 2.000 minutos grátis; repositório público não tem limite, mas deixa o estado e o config visíveis para todos.

## Comandos

```text
python -m xgrowth verificar     confere config, chaves e conexões
python -m xgrowth tick          um ciclo (é o que o agendador chama)
python -m xgrowth daemon        roda em loop no PC/servidor
python -m xgrowth plano         plano e rascunhos de hoje
python -m xgrowth revisar       aprova/descarta pendentes pelo terminal
python -m xgrowth relatorio     o que o otimizador aprendeu
python -m xgrowth simular       simulação de público
python -m xgrowth telegram-id   descobre o TELEGRAM_CHAT_ID
```

No Telegram (só no chat autorizado): `/fato <texto>` informa um fato verdadeiro para os próximos posts, `/fatos` lista os fatos em uso, `/esquecer` apaga os que você enviou.

Botão de pânico: `seguranca.pausado: true` no config (o robô não faz mais nada até você voltar para `false`). Outros limites: `max_posts_por_dia_absoluto`, `respostas.max_por_dia`, `respostas.max_por_usuario_por_dia`.

## Expectativa realista

Conta nova começa com alcance baixo: quase ninguém a segue, e boa parte da distribuição do X passa por quem já segue o autor. O algoritmo dá um empurrão a autores com poucas impressões, mas isso só vira crescimento quando o post segura a atenção. As primeiras semanas servem para o otimizador aprender; o crescimento aparece quando ele já sabe o que o seu público quer. O que mais acelera: um nicho bem definido, exemplos de voz bons no config e responder rápido a quem interage (aprovar pelo Telegram leva segundos).

## Estrutura

```text
xgrowth/engine.py     o ciclo: plano, rascunhos, publicação, métricas, respostas, relatório
xgrowth/bandit.py     regressão bayesiana + Thompson Sampling + memória adaptativa
xgrowth/learning.py   métricas → pontuação → observações
xgrowth/writer.py     prompts e chamadas ao Gemini
xgrowth/x_client.py   API v2 do X (e um cliente falso para o dry-run)
xgrowth/notifier.py   Telegram: aprovações e resumo diário
xgrowth/sim.py        simulação de público
tests/                38 testes (pytest), incluindo semanas simuladas de operação
```
