# Jarvis — assistente pessoal

Assistente de IA para conversar, debater ideias e ajudar a pensar. Construído em
camadas, inspirado (só na ideia) no JARVIS.

**Nível 1 (atual):** chatbot de terminal com histórico de conversa, usando a API
da Anthropic (modelo Claude Sonnet).

## Requisitos

- Python 3.12+
- Uma API key da Anthropic (https://platform.claude.com/settings/keys)

## Configuração

1. Crie o ambiente e instale as dependências:

   ```
   conda create -n jarvis python=3.12
   conda activate jarvis
   pip install -r requirements.txt
   ```

2. Defina a API key como variável de ambiente (nunca coloque a key no código):

   - **Windows:** Painel de Controle → "Editar as variáveis de ambiente da sua
     conta" → nova variável de usuário `ANTHROPIC_API_KEY` com o valor `sk-ant-...`
   - **Linux/macOS:** `export ANTHROPIC_API_KEY="sk-ant-..."`

   Feche e reabra o terminal (e o editor) depois de criar a variável.

## Uso

```
python jarvis.py
```

Digite `sair` para encerrar. O custo acumulado da sessão aparece a cada resposta.

### Scripts auxiliares

- `teste.py` — uma única chamada à API, para verificar se o ambiente está ok.
- `modelos.py` — lista os modelos disponíveis para a sua conta.

## Roadmap

- **Nível 2:** acesso a ferramentas (web, arquivos, código) via tool use.
- **Nível 3:** personalidade fixa e memória entre sessões.
