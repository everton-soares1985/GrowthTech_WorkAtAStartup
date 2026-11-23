# Work at a Startup APPLY V0

## Estado

**WIP — operacional somente sob supervisao apos validacao read-only.**

Esta V0 cobre o fluxo interno mais comum do site:

```text
Companies & Jobs
→ View job
→ leitura da descricao
→ Apply
→ textarea #apply-message
→ mensagem via IA
→ Send
→ confirmacao Applied
→ proxima vaga
```

Qualquer formulario diferente e fechado e pulado. A automacao nao abre Apply
externo e nao tenta resolver formularios complexos.

O modal simples pode conter apenas a mensagem, o botao `Send` e o checkbox
padrao de preferencia de localizacao. Qualquer outro campo visivel classifica o
formulario como nao padrao e impede a chamada da IA e o envio.

O sistema nao classifica compatibilidade e nao escolhe vagas. O usuario ajusta
os filtros na barra lateral e deixa a listagem aberta; o lote preserva essa URL
e tenta as vagas exibidas, na ordem. A IA escreve uma mensagem verdadeira para
cada formulario suportado, mesmo quando a experiencia vem de outra area.

As mensagens ficam entre 300 e 600 caracteres. Respostas longas sao reduzidas
em limite de frase ou palavra. Se os tres provedores falharem, uma mensagem
local segura usa a vaga, a empresa e competencias transferiveis verificadas
para que o formulario simples nao seja perdido.

## Browser dedicado

```powershell
cd C:\PROGRAMACAO\GrowthTech_WorkAtAStartup
.\scripts\start_chrome.bat
```

Depois que o login estiver salvo pelo modo visual, o mesmo perfil pode ser
iniciado sem janela:

```powershell
.\scripts\start_chrome.bat headless
```

- Porta CDP: `9226`
- Perfil do Chrome: `.chrome-profile` dentro do projeto, ignorado pelo Git
- Login: sempre manual
- Headless: reutiliza a sessao salva; nao resolve login, CAPTCHA ou challenge

Depois de abrir o navegador, ajuste manualmente os filtros desejados na barra
lateral e mantenha a pagina `Companies & Jobs` aberta. O comando de envio nao
substitui essa listagem por filtros proprios.

## IA

O sistema reutiliza `OPENROUTER_API_KEY` do ambiente ou do `.env` local ignorado
pelo Git. Nenhuma chave nova e armazenada.

Ordem:

1. `z-ai/glm-5.3-flash`
2. `tencent/hy4-flash`
3. `nvidia/nemotron-3-ultra-550b-a55b:free`

O contexto profissional vem exclusivamente de `config/profile.local.json`.
Telefone, e-mail, segredos e documentos sao removidos antes do payload.

O perfil inclui sistemas agenticos pela fonte de portfolio, alem de RAG e
desenvolvimento de sistemas end-to-end no perfil local.

## Comandos

Self-check sem browser ou rede:

```powershell
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py --self-check
```

Listagem read-only, sem abrir Apply:

```powershell
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py --list-readonly --max-jobs 10
```

Lote real supervisionado, somente apos QA:

```powershell
.\.venv\Scripts\python.exe -X utf8 src\workatastartup_apply.py `
  --apply-batch --execute-apply --max-jobs 5 `
  --env-file .env `
  --profile config\profile.local.json
```

`--max-jobs` representa candidaturas confirmadas. `--max-scan` limita quantas
URLs podem ser examinadas. Nao existe teto artificial no codigo: o operador
define os dois valores no comando. A quantidade real continua limitada pelas
vagas disponiveis, pelos formularios suportados e pelos ciclos de rolagem.

Vagas sem Apply interno, ja aplicadas ou com formulario fora do padrao V0 sao
registradas e puladas; isso e uma limitacao tecnica, nao uma decisao de perfil.

## Confirmacao e falhas

- Antes do Send, o textarea deve conter exatamente a mensagem validada.
- Depois do clique, o script aguarda 5 segundos e exige o estado `Applied`.
- Submit sem `Applied` gera estado incerto e interrompe o lote.
- Login, CAPTCHA e challenge interrompem o lote.
- Relatorios sanitizados ficam em `logs/`.

## Fora do escopo da V0

- formularios diferentes de `#apply-message + Send`;
- anexos, perguntas adicionais ou redirecionamento externo;
- agendamento, VPS ou execucao autonoma;
- ledger SQLite e retomada automatica.
