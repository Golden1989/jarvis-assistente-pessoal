"""
Jarvis - Nivel 1
Chatbot de terminal com historico de conversa, usando a API da Anthropic.
"""

import anthropic
from pathlib import Path

MODELO = "claude-sonnet-5"
MAX_TOKENS_RESPOSTA = 1024

# Precos do claude-sonnet-5 (US$ por milhao de tokens).
PRECO_ENTRADA = 2.0
PRECO_SAIDA = 10.0

# Arquivo opcional com informacoes sobre voce. Fica fora do Git (.gitignore).
ARQUIVO_PERFIL = "perfil.txt"

# Identidade base — vale mesmo sem o perfil.txt (ex.: repo recém-clonado).
# Tom, tratamento e detalhes pessoais ficam no perfil.txt.
SYSTEM_PROMPT_BASE = (
    "Você é o Jarvis, um assistente pessoal para conversar, debater ideias "
    "e ajudar a pensar. Responda em português do Brasil. Se houver uma seção "
    "de perfil abaixo, siga as preferências dela sobre tom e tratamento."
)

COMANDOS_SAIR = {"sair", "exit", "quit"}


def montar_system_prompt():
    """Junta a personalidade base com o seu perfil, se o arquivo existir."""
    prompt = SYSTEM_PROMPT_BASE
    arquivo = Path(ARQUIVO_PERFIL)
    if arquivo.exists():
        perfil = arquivo.read_text(encoding="utf-8").strip()
        if perfil:
            prompt += "\n\n# Sobre a pessoa com quem você conversa\n" + perfil
    return prompt


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

    system_prompt = montar_system_prompt()

    print("Jarvis (Nível 1) — digite 'sair' para encerrar.")
    if Path(ARQUIVO_PERFIL).exists():
        print(f"(perfil carregado de {ARQUIVO_PERFIL})")
    print()

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
                system=system_prompt,
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
