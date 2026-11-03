import os
import re
import json
import sqlite3
import gzip
import zipfile
import tempfile
import shutil
from datetime import datetime
from pathlib import Path
from collections import Counter
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao-123456")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024  # 300 MB

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "banco_dados.db"
FUNCIONARIOS_JSON = BASE_DIR / "funcionarios.json"
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"
UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# ====================== BANCO DE DADOS ======================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            criado_em TEXT
        );
        CREATE TABLE IF NOT EXISTS blocos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT UNIQUE,
            empresa TEXT,
            cnpj TEXT,
            empresa_id TEXT,
            agencias TEXT,
            usuarios TEXT,
            cpfs TEXT,
            celulares TEXT,
            conteudo TEXT,
            origem TEXT,
            criado_em TEXT
        );
    """)
    # Cria admin padrão se não existir
    if not conn.execute("SELECT 1 FROM usuarios WHERE username = 'admin'").fetchone():
        conn.execute(
            "INSERT INTO usuarios (username, password_hash, is_admin, criado_em) VALUES (?, ?, 1, ?)",
            ("admin", generate_password_hash("admin123"), datetime.now().isoformat())
        )
    conn.commit()
    conn.close()

def get_funcionarios():
    try:
        return sorted(set(x.strip().upper() for x in json.loads(FUNCIONARIOS_JSON.read_text(encoding="utf-8")) if x.strip()))
    except:
        return []

def save_funcionarios(lista):
    FUNCIONARIOS_JSON.write_text(json.dumps(sorted(set(lista)), ensure_ascii=False, indent=2), encoding="utf-8")

# ====================== FUNÇÕES DE PROCESSAMENTO ======================
def digits(s):
    return re.sub(r"\D", "", s or "")

def safe(s):
    return re.sub(r'[<>:"/\\|?*]', "", s).strip()

def blocos_nydo(t):
    L = t.splitlines()
    starts = [i for i, x in enumerate(L) if re.search(r"^\s*CNPJ\s*:", x)]
    out = []
    for k, s in enumerate(starts):
        ini = s
        if ini > 0 and re.match(r"CNPJ\s+\d+", L[ini-1].strip()):
            ini -= 1
        fim = starts[k+1] if k+1 < len(starts) else len(L)
        bloco = "\n".join(L[ini:fim]).strip() + "\n"
        if bloco.strip():
            out.append(bloco)
    return out

def meta_nydo(b):
    mc = re.search(r"CNPJ\s*:\s*([0-9./-]+)", b)
    me = re.search(r"RAZÃO SOCIAL\s*:\s*(.+)", b)
    mag = re.search(r"AGÊNCIA\s*:\s*(\d+)", b)
    empresa = me.group(1).strip() if me else ""
    cnpj = mc.group(1).strip() if mc else ""
    ag = mag.group(1).zfill(4) if mag else ""
    mcpf = re.search(r"CPF\s*:\s*([0-9.\-]+)", b)
    cpf = mcpf.group(1) if mcpf else ""
    mn = re.search(r"NOME\s*:\s*(.+)", b)
    nome = mn.group(1).strip() if mn else ""
    mtel = re.search(r"TEL\s*:\s*(.+?)\s*\|\s*GOV\s+ENCONTRADO", b, re.I)
    tel_raw = mtel.group(1).strip() if mtel else ""
    phones = re.findall(r"\((\d{2})\)\s*(\d{4,5})[-\s]?(\d{4})", tel_raw)
    tel = "".join(phones[0]) if phones else ""
    return empresa, cnpj, "", ag, nome, cpf, tel

def extrair_nydo(t):
    saida = []
    for bloco in blocos_nydo(t):
        empresa, cnpj, eid, ag, nome, cpf, tel = meta_nydo(bloco)
        if not tel or not ag or not nome:
            continue
        primeiro_nome = nome.split()[0].upper() if nome else ""
        saida.append((tel, ag, primeiro_nome))
    saida.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    return saida

def sem_cab(t):
    L = t.splitlines()
    i = next((i for i, x in enumerate(L) if re.search(r"^\s*EMPRESA\s*:", x)), None)
    if i is None:
        return t
    while i > 0 and (not L[i-1].strip() or re.fullmatch(r"\s*[═=\-]{5,}\s*", L[i-1])):
        i -= 1
    return "\n".join(L[i:]).rstrip() + "\n"

def blocos_empresa(t):
    L = sem_cab(t).splitlines()
    starts = [i for i, x in enumerate(L) if re.search(r"^\s*EMPRESA\s*:", x)]
    out = []
    for k, s in enumerate(starts):
        ini = s
        while ini > 0 and (not L[ini-1].strip() or re.fullmatch(r"\s*[═=\-]{5,}\s*", L[ini-1])):
            ini -= 1
        fim = starts[k+1] if k+1 < len(starts) else len(L)
        out.append("\n".join(L[ini:fim]).strip() + "\n")
    return out

def meta(b):
    me = re.search(r"^\s*EMPRESA\s*:\s*(.+)$", b, re.M)
    mc = re.search(r"^\s*CNPJ\s*:\s*([0-9./-]+).*?\bID:\s*(\d+)", b, re.M)
    empresa = me.group(1).strip() if me else ""
    cnpj = mc.group(1) if mc else ""
    eid = mc.group(2) if mc else ""
    ag = " | ".join(dict.fromkeys(re.findall(r"\bAg\s+(\d{1,6})\s*\|\s*Cc\b", b, re.I)))
    cpfs = " | ".join(dict.fromkeys(re.findall(r"\bCPF:\s*([0-9.-]{11,14})", b, re.I)))
    tels = " | ".join(dict.fromkeys("".join(x) for x in re.findall(r"\((\d{2})\)\s*(\d{4,5})[-\s]?(\d{4})", b)))
    users = []
    L = b.splitlines()
    for i, x in enumerate(L):
        if "Login:" in x and i:
            n = L[i-1].strip().lstrip("│").strip()
            if n and n not in users:
                users.append(n)
    return empresa, cnpj, eid, ag, " | ".join(users), cpfs, tels

def extrair(t):
    if "GOV ENCONTRADO" in t and "RAZÃO SOCIAL" in t:
        return extrair_nydo(t)
    saida = []
    for bloco in blocos_empresa(t):
        agencias = re.findall(r"\bAg\s+(\d{1,6})\s*\|\s*Cc\b", bloco, re.I)
        if not agencias:
            continue
        agencia = agencias[0].zfill(4)
        linhas = bloco.splitlines()
        candidatos = []
        for i, linha in enumerate(linhas):
            if not re.search(r"\bLogin\s*:", linha, re.I):
                continue
            if not re.search(r"\bPerfil\s*:\s*Master\b", linha, re.I):
                continue
            nome = linhas[i-1].strip().lstrip("│").strip() if i else ""
            if not nome:
                continue
            fim = next((j for j in range(i+1, len(linhas))
                        if re.search(r"\bLogin\s*:", linhas[j]) or
                        re.search(r"QUADRO SOCIET[ÁA]RIO", linhas[j], re.I)), len(linhas))
            secao = "\n".join(linhas[i:fim])
            celulares = re.search(r"\bCelulares\s*:(.*?)(?:\n|$)", secao, re.I)
            if not celulares:
                continue
            for m in re.finditer(r"\((\d{2})\)\s*(\d{4,5})[-\s]?(\d{4})\s*✓\s*GOV\b", celulares.group(1), re.I):
                candidatos.append(("".join(m.groups()), agencia, nome.split()[0].upper()))
        if candidatos:
            saida.append(candidatos[0])
    saida.sort(key=lambda x: int(x[0]) if x[0].isdigit() else 0)
    return saida

def ler_arquivo(caminho):
    p = Path(caminho)
    if p.name.lower().endswith(".txt.gz") or p.suffix.lower() == ".gz":
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            arquivos = [x for x in z.infolist() if not x.is_dir() and x.filename.lower().endswith(".txt") and not x.filename.startswith("__MACOSX/")]
            if not arquivos:
                raise ValueError("O ZIP não contém arquivos TXT.")
            return "\n".join(z.read(x).decode("utf-8", errors="replace") for x in arquivos)
    return p.read_text(encoding="utf-8", errors="replace")

def importar_blocos(texto, origem):
    conn = get_db()
    n = r = 0
    is_nydo = "GOV ENCONTRADO" in texto and "RAZÃO SOCIAL" in texto
    blocos = blocos_nydo(texto) if is_nydo else blocos_empresa(texto)
    for b in blocos:
        if is_nydo:
            empresa, cnpj, eid, ag, nome, cpf, tel = meta_nydo(b)
            u, cpfs, tels = nome, cpf, tel
        else:
            empresa, cnpj, eid, ag, u, cpfs, tels = meta(b)
        chave = eid or cnpj or empresa + "|" + b[:80]
        try:
            conn.execute(
                "INSERT INTO blocos(chave,empresa,cnpj,empresa_id,agencias,usuarios,cpfs,celulares,conteudo,origem,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (chave, empresa, cnpj, eid, ag, u, cpfs, tels, b, origem, datetime.now().isoformat(timespec="seconds"))
            )
            n += 1
        except sqlite3.IntegrityError:
            r += 1
    conn.commit()
    conn.close()
    return n, r

def processar_arquivo(caminho_arquivo, letra, funcionario):
    texto = ler_arquivo(caminho_arquivo)
    is_nydo = "GOV ENCONTRADO" in texto and "RAZÃO SOCIAL" in texto
    todos = blocos_nydo(texto) if is_nydo else blocos_empresa(texto)

    conn = get_db()
    existentes = {r[0] for r in conn.execute("SELECT chave FROM blocos")}
    conn.close()

    novos = []
    vistos = set()
    repetidos = 0
    for b in todos:
        if is_nydo:
            empresa, cnpj, eid, *_ = meta_nydo(b)
            chave = eid or cnpj or empresa + "|" + b[:80]
        else:
            empresa, cnpj, eid, *_ = meta(b)
            chave = eid or cnpj or empresa + "|" + b[:100]
        if chave in existentes or chave in vistos:
            repetidos += 1
            continue
        vistos.add(chave)
        novos.append(b)

    if not novos:
        return 0, None, None, None, 0, 0, repetidos

    original = "\n".join(b.rstrip() for b in novos) + "\n"
    R = extrair(original)
    now = datetime.now()
    dc = now.strftime("%d/%m")
    contagem_ddd = Counter(tel[:2] for tel, ag, n in R if len(tel) >= 10)
    ddd = max(contagem_ddd, key=lambda x: (contagem_ddd[x], -int(x))) if contagem_ddd else "SEM-DDD"
    prefixo = f"DDD{ddd}" if ddd != "SEM-DDD" else ddd
    base = safe(f"{prefixo} - {funcionario} - {len(R)} INFO - {now:%d-%m-%Y} - {now:%H-%M}")

    # Salva no banco
    n, r = importar_blocos(original, base + " - ORIGINAL.txt")

    # Gera arquivos temporários
    out_dir = OUTPUT_FOLDER / session.get("username", "user") / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    original_path = out_dir / f"{base} - ORIGINAL.txt"
    filtrado_path = out_dir / f"{base} - FILTRADO.txt"
    vcf_path = out_dir / f"{base}.vcf"

    original_path.write_text(original, encoding="utf-8")
    filtrado_path.write_text("\n".join(f"{tel};{ag}{letra}-{nome} {dc}" for tel, ag, nome in R) + ("\n" if R else ""), encoding="utf-8")

    def vcf_escape(value):
        return str(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")

    cards = []
    for tel, ag, nome in R:
        fn = f"{ag}{letra}-{nome} {dc}"
        cards += ["BEGIN:VCARD", "VERSION:3.0", f"FN:{vcf_escape(fn)}", f"N:;{vcf_escape(fn)};;;", f"TEL;TYPE=CELL:+55{tel}", "END:VCARD"]
    vcf_path.write_bytes(("\r\n".join(cards) + ("\r\n" if cards else "")).encode("utf-8"))

    return len(R), original_path, filtrado_path, vcf_path, n, r, repetidos

# ====================== AUTENTICAÇÃO ======================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        conn = get_db()
        user = conn.execute("SELECT is_admin FROM usuarios WHERE username = ?", (session["username"],)).fetchone()
        conn.close()
        if not user or not user["is_admin"]:
            flash("Acesso restrito a administradores.")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated

# ====================== ROTAS ======================
@app.route("/")
def index():
    if "username" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        user = conn.execute("SELECT * FROM usuarios WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["username"] = username
            session["is_admin"] = bool(user["is_admin"])
            return redirect(url_for("dashboard"))
        flash("Usuário ou senha inválidos.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", username=session["username"], is_admin=session.get("is_admin"))

@app.route("/processar", methods=["GET", "POST"])
@login_required
def processar():
    funcionarios = get_funcionarios()
    if request.method == "POST":
        if "arquivo" not in request.files:
            flash("Nenhum arquivo enviado.")
            return redirect(request.url)
        arquivo = request.files["arquivo"]
        if arquivo.filename == "":
            flash("Nenhum arquivo selecionado.")
            return redirect(request.url)
        funcionario = request.form.get("funcionario", "").strip().upper()
        letra = request.form.get("letra", "N")
        if not funcionario:
            flash("Selecione ou informe o funcionário.")
            return redirect(request.url)

        # Salva upload
        filename = secure_filename(arquivo.filename)
        upload_path = UPLOAD_FOLDER / f"{session['username']}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"
        arquivo.save(upload_path)

        try:
            q, o, f, v, n, r, rep = processar_arquivo(upload_path, letra, funcionario)
            if o is None:
                flash(f"Nenhum registro novo. {rep} empresas repetidas ignoradas.")
            else:
                session["last_files"] = {
                    "original": str(o),
                    "filtrado": str(f),
                    "vcf": str(v),
                    "q": q,
                    "n": n,
                    "rep": rep
                }
                flash(f"Processado! {q} contatos | {n} empresas novas | {rep} repetidas.")
                return redirect(url_for("resultado"))
        except Exception as e:
            flash(f"Erro ao processar: {str(e)}")
        finally:
            if upload_path.exists():
                upload_path.unlink(missing_ok=True)
    return render_template("processar.html", funcionarios=funcionarios)

@app.route("/resultado")
@login_required
def resultado():
    files = session.get("last_files")
    if not files:
        return redirect(url_for("processar"))
    return render_template("resultado.html", files=files)

@app.route("/download/todos")
@login_required
def download_todos():
    files = session.get("last_files", {})
    paths = [files.get("original"), files.get("filtrado"), files.get("vcf")]
    paths = [p for p in paths if p and Path(p).exists()]
    if len(paths) < 3:
        flash("Arquivos não encontrados.")
        return redirect(url_for("processar"))

    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, Path(p).name)
    buffer.seek(0)
    return send_file(
        buffer,
        as_attachment=True,
        download_name="arquivos_gov.zip",
        mimetype="application/zip"
    )

@app.route("/download/<tipo>")
@login_required
def download(tipo):
    files = session.get("last_files", {})
    path = files.get(tipo)
    if not path or not Path(path).exists():
        flash("Arquivo não encontrado.")
        return redirect(url_for("processar"))
    return send_file(path, as_attachment=True)

@app.route("/pesquisa", methods=["GET", "POST"])
@login_required
def pesquisa():
    resultados = []
    if request.method == "POST":
        q = request.form.get("q", "").strip()
        if q:
            conn = get_db()
            rows = conn.execute("SELECT id, empresa, cnpj, empresa_id, conteudo FROM blocos").fetchall()
            conn.close()
            dq = digits(q)
            for row in rows:
                ok = False
                if dq and len(dq) >= 4:
                    ok = dq in digits(row["conteudo"])
                else:
                    ok = q.casefold() in (row["conteudo"] or "").casefold()
                if ok:
                    resultados.append(dict(row))
            resultados = resultados[:200]
    return render_template("pesquisa.html", resultados=resultados)

@app.route("/ver/<int:rid>")
@login_required
def ver(rid):
    conn = get_db()
    row = conn.execute("SELECT * FROM blocos WHERE id = ?", (rid,)).fetchone()
    conn.close()
    if not row:
        flash("Registro não encontrado.")
        return redirect(url_for("pesquisa"))
    return render_template("ver.html", registro=row)

@app.route("/admin/usuarios", methods=["GET", "POST"])
@admin_required
def admin_usuarios():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "criar":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            is_admin = 1 if request.form.get("is_admin") else 0
            if username and password:
                try:
                    conn.execute(
                        "INSERT INTO usuarios (username, password_hash, is_admin, criado_em) VALUES (?, ?, ?, ?)",
                        (username, generate_password_hash(password), is_admin, datetime.now().isoformat())
                    )
                    conn.commit()
                    flash("Usuário criado com sucesso.")
                except sqlite3.IntegrityError:
                    flash("Usuário já existe.")
        elif action == "excluir":
            uid = request.form.get("uid")
            if uid:
                conn.execute("DELETE FROM usuarios WHERE id = ? AND username != 'admin'", (uid,))
                conn.commit()
                flash("Usuário excluído.")
    usuarios = conn.execute("SELECT id, username, is_admin, criado_em FROM usuarios").fetchall()
    conn.close()
    return render_template("admin_usuarios.html", usuarios=usuarios)

@app.route("/admin/importar-banco", methods=["GET", "POST"])
@admin_required
def importar_banco():
    if request.method == "POST":
        if "banco" not in request.files:
            flash("Nenhum arquivo enviado.")
            return redirect(request.url)
        arquivo = request.files["banco"]
        if arquivo.filename == "" or not arquivo.filename.endswith(".db"):
            flash("Envie um arquivo .db válido.")
            return redirect(request.url)
        
        temp_path = BASE_DIR / "banco_temp.db"
        arquivo.save(temp_path)
        
        try:
            test_conn = sqlite3.connect(temp_path)
            test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            test_conn.close()
            
            if DB_PATH.exists():
                DB_PATH.unlink()
            shutil.move(str(temp_path), str(DB_PATH))
            flash("Banco de dados importado com sucesso!")
        except Exception as e:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            flash(f"Erro ao importar banco: {str(e)}")
        return redirect(url_for("admin_usuarios"))
    
    return render_template("importar_banco.html")

@app.route("/funcionarios", methods=["GET", "POST"])
@login_required
def funcionarios():
    lista = get_funcionarios()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "adicionar":
            nome = safe(request.form.get("nome", "")).upper()
            if nome and nome not in lista:
                lista.append(nome)
                save_funcionarios(lista)
                flash("Funcionário adicionado.")
        elif action == "remover":
            nome = request.form.get("nome")
            if nome in lista:
                lista.remove(nome)
                save_funcionarios(lista)
                flash("Funcionário removido.")
        return redirect(url_for("funcionarios"))
    return render_template("funcionarios.html", funcionarios=lista)

# Inicializa o banco ao carregar o app (funciona com gunicorn)
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
