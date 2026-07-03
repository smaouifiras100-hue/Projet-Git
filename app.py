import os
import requests
import pandas as pd
import traceback
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from fpdf import FPDF

# ==========================================
# 1. CONFIGURATION & INITIALISATION
# ==========================================
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'cle_secours_temporaire')
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'lebonchiffre.db')
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
app.config['ALLOWED_EXTENSIONS'] = {'pdf', 'xlsx', 'xls', 'docx', 'jpg', 'png'}

for f in [app.config['UPLOAD_FOLDER'], os.path.join(app.config['UPLOAD_FOLDER'], 'academy'), os.path.join(app.config['UPLOAD_FOLDER'], 'templates_lettres')]:
    if not os.path.exists(f): os.makedirs(f)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login_page'

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']
# ==========================================
# 2. MODÈLES DE DONNÉES COMPLETS
# ==========================================
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    entreprise = db.Column(db.String(100))
    pays_juridiction = db.Column(db.String(2), default='TN')
    score = db.Column(db.Integer, default=0)
    
    documents = db.relationship('Document', backref='owner', lazy=True)
    signatures = db.relationship('ContractSignature', backref='signataire', lazy=True)

    def set_password(self, pwd): self.password = bcrypt.generate_password_hash(pwd).decode('utf-8')
    def check_password(self, pwd): return bcrypt.check_password_hash(self.password, pwd)

class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nom_fichier = db.Column(db.String(200), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    statut = db.Column(db.String(50), default="Reçu")

class ContractSignature(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    mission_id = db.Column(db.String(100))
    signature_data = db.Column(db.Text)

class AuditData(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    client_name = db.Column(db.String(100))
    secteur = db.Column(db.String(50))
    raw_data = db.Column(db.JSON)
    conclusions = db.relationship('AuditConclusion', backref='source_data', lazy=True)

class AuditConclusion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    audit_data_id = db.Column(db.Integer, db.ForeignKey('audit_data.id'))
    conclusion_synthese = db.Column(db.Text)
    date_analyse = db.Column(db.DateTime, default=datetime.utcnow)

class SemanticRelation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    secteur = db.Column(db.String(50))
    sujet = db.Column(db.String(100))
    predicate = db.Column(db.String(100))
    objet = db.Column(db.String(100))

class KnowledgeBase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    juridiction = db.Column(db.String(2))
    contenu = db.Column(db.Text)

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    titre = db.Column(db.String(200))
    description = db.Column(db.Text)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    contenu = db.Column(db.Text, nullable=False)
    expediteur_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    destinataire_id = db.Column(db.Integer, db.ForeignKey('user.id'))

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ==========================================
# 3. UTILITAIRES, IA & ONTOLOGIES
# ==========================================
class UnicodePDF(FPDF):
    def header(self):
        try:
            self.add_font('DejaVu', '', 'fonts/DejaVuSans.ttf', uni=True)
            self.set_font('DejaVu', '', 12)
        except:
            self.set_font('Arial', '', 12)
        self.cell(0, 10, "SF CONSULTING & EXPERTISE - RAPPORT D'AUDIT", 0, 1, 'R')
        self.ln(10)

class AgentAuditSFCE:
    def __init__(self, secteur):
        self.secteur = secteur
        self.system_prompt = (
            "Tu es un Expert-comptable Inscrit à l'OECT (TN) & CNO (Paris-IDF) agissant avec un scepticisme professionnel absolu. "
            "Examine les données pour identifier toute non-conformité. Distingue les erreurs matérielles de ce qui pourrait constituer une transgression créatrice dans les modèles de gestion, tout en restant dans le strict cadre légal."
        )

    def analyser(self, raw_lines):
        relations = SemanticRelation.query.filter_by(secteur=self.secteur).all()
        ontologie_texte = "\n".join([f"- {r.sujet} {r.predicate} {r.objet}" for r in relations])
        prompt = f"SYSTEM: {self.system_prompt}\n\nAnalyse sectorielle ({self.secteur}):\n{ontologie_texte}\n\nDonnées: {raw_lines}"
        
        try:
            res = requests.post("http://127.0.0", json={"model": "phi3", "prompt": prompt, "stream": False}, timeout=600)
            return res.json().get('response', "Erreur d'analyse.")
        except Exception as e:
            return f"Moteur IA local hors ligne: {str(e)}"

def initialiser_ontologies_sectorielles():
    ontologies = {
        'Agricole': [
            SemanticRelation(secteur='Agricole', sujet='ActifBiologique', predicate='doit_etre_valorise_selon', objet='IAS_41'),
            SemanticRelation(secteur='Agricole', sujet='SubventionExploitation', predicate='doit_corroborer_avec', objet='EngagementDeDurabilite')
        ],
        'Industriel': [
            SemanticRelation(secteur='Industriel', sujet='FluxStocks', predicate='doit_etre_audite_via', objet='MethodeInventairePermanent'),
            SemanticRelation(secteur='Industriel', sujet='AmortissementMachine', predicate='doit_respecter', objet='DureeVieEconomique_LCA')
        ],
        'Services_Conseil': [
            SemanticRelation(secteur='Services_Conseil', sujet='BureauControle', predicate='doit_garantir_impartialite_selon', objet='ISO_17020'),
            SemanticRelation(secteur='Services_Conseil', sujet='HonorairesConseil', predicate='doit_justifier_par', objet='Timesheet_Validé')
        ],
        'BTP': [
            SemanticRelation(secteur='BTP', sujet='ContratChantier', predicate='doit_appliquer', objet='Avancement_Pourcentage'),
            SemanticRelation(secteur='BTP', sujet='SousTraitance', predicate='doit_verifier', objet='AttestationVigilance')
        ],
        'Bancaire_Financier': [
            SemanticRelation(secteur='Bancaire_Financier', sujet='OperationFactoring', predicate='doit_valider', objet='NotificationDebiteur'),
            SemanticRelation(secteur='Bancaire_Financier', sujet='PaiementPlateforme', predicate='doit_respecter', objet='DSP2_Securite'),
            SemanticRelation(secteur='Bancaire_Financier', sujet='ContratLeasing', predicate='doit_etre_comptabilise', objet='IFRS_16'),
            SemanticRelation(secteur='Bancaire_Financier', sujet='ContratAssurance', predicate='doit_respecter', objet='Solvabilite_II')
        ]
    }
    try:
        for secteur, regles in ontologies.items():
            for r in regles:
                existe = SemanticRelation.query.filter_by(secteur=secteur, sujet=r.sujet, predicate=r.predicate, objet=r.objet).first()
                if not existe:
                    db.session.add(r)
        db.session.commit()
    except Exception as e:
        print(f"Erreur initialisation ontologies: {e}")

# ==========================================
# 4. ROUTES ET VUES FLASK
# ==========================================
@app.route('/')
def index():
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
        
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):
            login_user(user)
            flash('Connexion réussie !', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Identifiants incorrects.', 'danger')
            
    return render_template('login.html')

@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        # 1. Vérification des entrées
        secteur = request.form.get('secteur')
        if 'file' not in request.files or not secteur:
            flash("Formulaire incomplet.", "danger")
            return redirect(request.url)
            
        file = request.files['file']
        if file.filename == '' or not allowed_file(file.filename):
            flash("Fichier manquant ou format non autorisé.", "danger")
            return redirect(request.url)

        try:
            # 2. Sécurisation et enregistrement du fichier
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_")
            unique_filename = timestamp + filename
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(file_path)

            # 3. Enregistrement initial du document en BDD
            doc = Document(nom_fichier=filename, user_id=current_user.id, statut="En cours d'analyse")
            db.session.add(doc)
            db.session.commit()

            # 4. Extraction rapide des données (Exemple avec un fichier texte ou Excel via Pandas)
            raw_lines = ""
            if filename.endswith(('.xlsx', '.xls')):
                df = pd.read_excel(file_path)
                raw_lines = df.to_string()
            else:
                # Lecture brute pour les fichiers texte/simples (Le traitement PDF complet sera géré via FPDF/pypdf)
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    raw_lines = f.read(2000) # Analyse des 2000 premiers caractères pour l'IA

            # 5. Enregistrement des données d'audit brutes
            audit_data = AuditData(user_id=current_user.id, client_name="Client_Anonyme", secteur=secteur, raw_data={"contenu": raw_lines[:5000]})
            db.session.add(audit_data)
            db.session.commit()

            # 6. Appel de l'Agent d'IA SFCE avec les ontologies sectorielles
            agent = AgentAuditSFCE(secteur=secteur)
            analyse_resultat = agent.analyser(raw_lines=raw_lines[:2000])

            # 7. Sauvegarde des conclusions de l'IA
            conclusion = AuditConclusion(audit_data_id=audit_data.id, conclusion_synthese=analyse_resultat)
            doc.statut = "Analyse Terminée"
            db.session.add(conclusion)
            db.session.commit()

            flash(f"Analyse réussie pour le secteur {secteur} !", "success")

        except Exception as e:
            db.session.rollback()
            traceback.print_exc()
            flash(f"Erreur lors du traitement : {str(e)}", "danger")

    # Récupération de l'historique de l'utilisateur pour l'affichage
    documents_utilisateurs = Document.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', documents=documents_utilisateurs)



@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login_page'))

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        initialiser_ontologies_sectorielles()
    app.run(debug=True)
