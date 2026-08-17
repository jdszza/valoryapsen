-- ============================================================
-- APSEN – bootstrap do MySQL
--
-- ESTE ARQUIVO NÃO DECLARA SCHEMA.
--
-- O docker-entrypoint só executa /docker-entrypoint-initdb.d na PRIMEIRA
-- criação do volume `mysql_data`. Um `docker compose down` sem `-v`, ou um
-- MySQL externo já existente, nunca passam por aqui — e é justamente aí que
-- um DDL duplicado aqui diverge do que o central espera.
--
-- Fonte de verdade do schema: `_DDL_TABELAS`, `_COLUNAS_EVOLUTIVAS` e os
-- seeds em central-computer/database.py, que rodam em TODO startup do
-- central. Motivação em CLAUDE.md, seção "Schema do banco".
--
-- tests/test_schema.py falha se voltar a existir CREATE TABLE / INSERT aqui.
-- ============================================================

-- A variável MYSQL_DATABASE do compose já cria o banco, mas com a collation
-- padrão do servidor (utf8mb4_0900_ai_ci no MySQL 8). Nomes de medicamento
-- são comparados por igualdade em várias queries; fixamos a collation para
-- que o comportamento não dependa da versão da imagem.
CREATE DATABASE IF NOT EXISTS apsen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

ALTER DATABASE apsen_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
