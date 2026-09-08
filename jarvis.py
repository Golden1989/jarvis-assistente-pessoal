"""
Jarvis - Nivel 1
Chatbot de terminal com historico de conversa, usando a API da Anthropic.
"""

import anthropic

MODELO = "claude-sonnet-5"
MAX_TOKENS_RESPOSTA = 1024

# Precos do claude-sonnet-5 (US$ por milhao de tokens).
PRECO_ENTRADA = 2.0
PRECO_SAIDA = 10.0

# "Personalidade" do assistente. Vale para a conversa inteira.
SYSTEM_PROMPT = (
    "Você é o Jarvis, um assistente pessoal para conversar, debater ideias "
    "e ajudar a pensar com clareza. Seja direto e honesto, aponte falhas no "
    "raciocínio quando existirem e faça perguntas quando algo estiver ambíguo. "
    "Responda em português do Brasil."
)

COMANDOS_SAIR = {"sair", "exit", "quit"}


def extrair_texto(resposta):
    """A resposta vem como uma lista de blocos; junta os blocos de texto."""
    return "\n".join(
        bloco.text for bloco in resposta.content if bloco.type == "text"
    )


def main():
    client = anthropic.Anthropic()  # lê ANTHROPIC_API_KEY do ambiente

    mensagens = []       # o histórico da conversa
    tokens_entrada = 0   # acumuladores para acompanhar o gasto da sessão
    tokens_saida = 0

    print("Jarvis (Nível 1) — digite 'sair' para encerrar.\n")

    while True:
        try:
            entrada = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not entrada:
            continue
        if entrada.lower() in COMANDOS_SAIR:
            break

        # 1. Adiciona a fala do usuário ao histórico.
        mensagens.append({"role": "user", "content": entrada})

        # 2. Envia o histórico INTEIRO e pede a resposta.
        try:
            resposta = client.messages.create(
                model=MODELO,
                max_tokens=MAX_TOKENS_RESPOSTA,
                system=SYSTEM_PROMPT,
                messages=mensagens,
            )
        except anthropic.APIError as erro:
            print(f"\n[erro na chamada: {erro}]\n")
            mensagens.pop()  # desfaz a última fala p/ o histórico não ficar torto
            continue

        # 3. Extrai o texto e 4. guarda no histórico p/ a próxima rodada ter contexto.
        texto = extrair_texto(resposta)
        mensagens.append({"role": "assistant", "content": texto})
        print(f"\nJarvis: {texto}\n")

        # 5. Atualiza e mostra o gasto acumulado da sessão.
        tokens_entrada += resposta.usage.input_tokens
        tokens_saida += resposta.usage.output_tokens
        custo = (
            tokens_entrada / 1_000_000 * PRECO_ENTRADA
            + tokens_saida / 1_000_000 * PRECO_SAIDA
        )
        print(
            f"[sessão: {tokens_entrada} entrada + {tokens_saida} saída tokens"
            f"  ~US$ {custo:.4f}]\n"
        )

    print("Até mais.")


if __name__ == "__main__":
    main()
