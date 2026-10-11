# xgrowth — perfil no X no piloto automático, com testes que se ajustam sozinhos

Um robô que escreve os posts (com o Claude), escolhe os horários, publica, mede o resultado de cada post 24h depois e usa isso para decidir o próximo: quais temas, formatos, tons, aberturas e horários dão mais views, respostas, compartilhamentos e cliques no perfil. Ele também responde quem interage com o perfil, dentro das regras do X.

Tudo roda sozinho a cada 30 minutos no GitHub Actions (grátis) ou no seu PC. Você só define o nicho e a voz uma vez.

## O que ele faz

O ciclo é: **planejar → escrever → publicar → medir → aprender → repetir**.

Todo dia o robô escolhe os horários (4 por padrão, entre 7h e 23h, com 2h de intervalo). Uma hora antes de cada um, escolhe a combinação de pilar de conteúdo × formato × tom × gancho de abertura e pede o post ao Claude, passando os posts recentes (para não se repetir) e os que mais deram certo (para aprender o estilo que funciona). Cada rascunho passa por uma checagem automática antes de sair: limite de caracteres, sem links, sem @menções, no máximo 1 hashtag, sem "dá like/me siga" e sem ser parecido demais com algo recente. Se falhar, volta para o Claude com o motivo.

Depois de publicado, o robô coleta views, curtidas, respostas, reposts, citações, salvamentos e cliques no perfil. Com 24h, o post vira um dado para o otimizador.

Respostas: quando alguém responde a um post ou menciona o perfil, o Claude escreve uma resposta curta e específica (ou decide não responder spam, provocação ou assunto sensível). Por padrão ela chega no seu Telegram com botões ✅/❌ e sai com um toque. Quem escreve "pare"/"stop" nunca mais recebe resposta automática.

Todo dia de manhã chega um resumo no Telegram, e o arquivo `data/relatorio.md` mostra o que o otimizador aprendeu (melhores horários, formatos, pilares, tons, ganchos e posts).

## O que ele não faz, de propósito

Nada disso é limitação técnica: é o que faz uma conta ser suspensa. Pelas [diretrizes de desenvolvedor do X](https://docs.x.com/developer-guidelines), contas automatizadas não podem curtir, seguir ou dar follow-back automaticamente, responder a quem não interagiu primeiro (por palavra-chave, por exemplo), postar em assuntos do momento para ganhar visibilidade, usar várias contas com o mesmo conteúdo ou automatizar pelo navegador em vez da API. E **respostas geradas por IA exigem aprovação prévia do X**, mesmo quando são úteis. Por isso as respostas começam no modo "aprovar".

Também evitamos o que o próprio algoritmo pune: o código aberto do ranking do X ([xai-org/x-algorithm](https://github.com/xai-org/x-algorithm)) mostra que bloqueios, "não tenho interesse" e denúncias derrubam a visibilidade da conta, então o prompt proíbe polêmica gratuita e ataques. Links ficam de fora porque custam 13× mais na API e costumam reduzir o alcance.

## Passo a passo

**1. Crie a conta no X (manual).** Nome, foto, capa e uma bio que diga que é automatizada, por exemplo: *"Dicas de PC gamer todo dia · conta automatizada gerenciada por @seuperfil"*. Depois ative o rótulo de conta automatizada nas configurações da conta (seção de informações da conta → Automação) e vincule ao seu perfil pessoal, que é obrigatório.

**2. Crie o acesso à API.** Entre em [console.x.com](https://console.x.com) **logado na conta do robô** (assim a leitura das próprias métricas custa US$ 0,001 por post em vez de US$ 0,005). Crie um app, em *User authentication settings* marque **Read and Write**, e só então gere *API Key/Secret* e *Access Token/Secret* (tokens gerados antes da permissão ficam só leitura). Compre créditos (US$ 10 duram cerca de um mês) e defina um limite de gasto.

**3. Chave do Claude.** Crie em [console.anthropic.com](https://console.anthropic.com).

**4. Telegram (opcional, recomendado).** No Telegram, fale com o @BotFather, use `/newbot` e guarde o token. Mande `/start` para o seu bot novo e rode `python -m xgrowth telegram-id` para descobrir o `TELEGRAM_CHAT_ID`. O robô só aceita cliques vindos desse chat.

**5. Configure.** Copie `config.example.yaml` para `config.yaml` e edite o nicho, o público, a voz e os pilares. Colar de 3 a 10 posts de exemplo da voz que você quer em `exemplos_de_posts` faz muita diferença. Copie `.env.example` para `.env` e preencha as chaves.

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

Cada post é uma combinação de cinco escolhas: pilar, formato (opinião curta, pergunta, lista, thread, cenário, comparação, mito vs. fato), tom, gancho de abertura e horário. Depois de 24h, ele ganha uma pontuação: `log(views) + log(engajamento ponderado)`, em que resposta vale 3, repost e citação 4, salvamento 2, curtida 1 e clique no perfil 5 (é o melhor indicador de novo seguidor). O log impede que um post viral isolado distorça tudo.

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

API do X (pay-per-use, [preços oficiais](https://docs.x.com/x-api/getting-started/pricing)): US$ 0,015 por post ou resposta, US$ 0,20 se tiver link, US$ 0,001 por post lido da própria conta. Com 4 posts por dia (algumas threads) e cerca de 10 respostas, fica perto de **US$ 8 a 10 por mês**. O X dá US$ 20 em créditos ao salvar o primeiro cartão.

Claude: poucas chamadas curtas por dia; confira o preço atual no console da Anthropic. Para gastar menos, use `claude-haiku-5-5` em `llm.modelo`.

GitHub Actions: cerca de 1.440 minutos por mês, dentro dos 2.000 gratuitos de repositórios privados.

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

Botão de pânico: `seguranca.pausado: true` no config (o robô não faz mais nada até você voltar para `false`). Outros limites: `max_posts_por_dia_absoluto`, `respostas.max_por_dia`, `respostas.max_por_usuario_por_dia`.

## Expectativa realista

Conta nova começa com alcance baixo: quase ninguém a segue, e boa parte da distribuição do X passa por quem já segue o autor. O algoritmo dá um empurrão a autores com poucas impressões, mas isso só vira crescimento quando o post segura a atenção. As primeiras semanas servem para o otimizador aprender; o crescimento aparece quando ele já sabe o que o seu público quer. O que mais acelera: um nicho bem definido, exemplos de voz bons no config e responder rápido a quem interage (aprovar pelo Telegram leva segundos).

## Estrutura

```text
xgrowth/engine.py     o ciclo: plano, rascunhos, publicação, métricas, respostas, relatório
xgrowth/bandit.py     regressão bayesiana + Thompson Sampling + memória adaptativa
xgrowth/learning.py   métricas → pontuação → observações
xgrowth/writer.py     prompts e chamadas ao Claude
xgrowth/x_client.py   API v2 do X (e um cliente falso para o dry-run)
xgrowth/notifier.py   Telegram: aprovações e resumo diário
xgrowth/sim.py        simulação de público
tests/                30 testes (pytest), incluindo semanas simuladas de operação
```
