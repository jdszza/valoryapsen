/*
 * =====================================================================
 *  CATALOGO DAS IMAGENS DAS TELAS — nome do medicamento -> numero da imagem
 * =====================================================================
 *  A tela entende NUMERO (o `imgN` de `imagens.h`); o comando `slot` traz o
 *  NOME, exatamente como esta em `medicamentos.nome` no central ("ALOIS 10MG").
 *  A traducao mora AQUI, ao lado das imagens, e em lugar nenhum mais.
 *
 *  Por que aqui e nao no adapter (mandando um campo `imagem` no `slot`): a
 *  ORDEM das imagens e um fato DESTA pasta — quem gera o `imagens.h` decide
 *  que a imagem 5 e o DESOL. Uma tabela de indices no adapter seria um segundo
 *  lugar guardando o mesmo fato, e a divergencia nao da erro: da a caixa de
 *  outro medicamento na tela do slot, que e exatamente o quadro de um
 *  medicamento trocado. Com a tabela aqui, trocar uma imagem e trocar a linha
 *  correspondente NO MESMO COMMIT, no mesmo arquivo que alguem esta olhando.
 *
 *  REGRAS (cobradas por tests/test_telas_imagens.py):
 *    * a posicao na lista E o numero da imagem: NOMES_IMAGEM[0] desenha img0;
 *    * a lista tem exatamente tantos nomes quanto `imgN` em imagens.h, e
 *      img0..imgN-1 sem buraco — o static_assert do sketch cobra a contagem
 *      na compilacao, o teste cobra o resto;
 *    * todo nome existe no catalogo do central (`_MEDICAMENTOS_SEED`), grafado
 *      igual. Um nome que nao casa nunca acha imagem, e a tela cai no modo
 *      texto sem erro em lugar nenhum.
 *
 *  A comparacao ignora maiusculas/minusculas e espacos repetidos ou nas
 *  pontas (`normalizarNome` no sketch) — e so isso. Nada de "parecido": casar
 *  "XAFAC 10MG" com "XAFAC 15MG" por aproximacao poria a caixa errada na tela.
 *  Medicamento sem imagem aparece como TEXTO, visivelmente diferente.
 * =====================================================================
 */
#pragma once

static const char* const NOMES_IMAGEM[] = {
  "ALOIS 10MG",           //  0
  "ARPADOL 400MG",        //  1
  "ATENTAH 18MG",         //  2
  "COBI-12 1000MCG",      //  3
  "COLCHIS 0,5MG",        //  4
  "DESOL",                //  5
  "DOBEVEN 500MG",        //  6
  "DONAREN 50MG",         //  7
  "DUEPOLI ER 500MG",     //  8
  "EXTIMA CHOCOLATE",     //  9
  "FLANCOX 500MG",        // 10
  "FLORACOL",             // 11
  "INILOK 40MG",          // 12
  "INPRUV DK 7000UI",     // 13
  "INSIT 50MG",           // 14
  "LACTOSIL 4500 COMP",   // 15
  "LACTOSIL 10000 COMP",  // 16
  "LACTOSIL FLORA",       // 17
  "LECZA XR 500MG",       // 18
  "LENIX 50MG",           // 19
  "LEVOXIN 500MG",        // 20
  "LEVOXIN 750MG",        // 21
  "LONIUM 40MG",          // 22
  "LURATT 20MG",          // 23
  "MAG B",                // 24
  "MECLIN 25MG",          // 25
  "MIOSAN 5MG",           // 26
  "MOTILEX",              // 27
  "PAXORAL 7MG",          // 28
  "PROBIANS",             // 29
  "PROBID",               // 30
  "RETEMIC 5MG",          // 31
  "SIL-HP 4MG",           // 32
  "SIL-HP 8MG",           // 33
  "UNOPROST 2MG",         // 34
  "XAFAC 2,5MG",          // 35
  "XAFAC 10MG",           // 36
  "XAFAC 20MG",           // 37
  "ZANIDIP 10MG",         // 38
};

#define NUM_IMAGENS ((int)(sizeof(NOMES_IMAGEM) / sizeof(NOMES_IMAGEM[0])))

// Dimensao de TODA imagem de imagens.h: tela inteira, retrato (rotacao 0).
#define IMAGEM_LARGURA 128
#define IMAGEM_ALTURA  160
