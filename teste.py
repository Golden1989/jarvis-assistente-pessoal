import anthropic

# Cria o cliente. Sem argumentos: ele lê ANTHROPIC_API_KEY do ambiente.
client = anthropic.Anthropic()

resposta = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=200,
    messages=[
        {"role": "user", "content": "Em uma frase, o que é a linguagem Python?"}
    ],
)

print(resposta.content[0].text)
print("---")
print("Tokens de entrada:", resposta.usage.input_tokens)
print("Tokens de saída: ", resposta.usage.output_tokens)