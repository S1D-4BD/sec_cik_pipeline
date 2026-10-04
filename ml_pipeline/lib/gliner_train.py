"""
Fine-tuning de GLiNER, suivi avec MLflow.
Les données doivent déjà être prêtes (train.json et eval.json).
Nécessite evaluate_gliner.py dans le même dossier.

Installation :
    pip install "gliner[training]" mlflow

Usage :
    python train_gliner.py --train train.json --eval eval.json --run-name essai_1
    mlflow ui --port 5001          # puis ouvrir http://127.0.0.1:5001
"""

import argparse
import glob
import json
import shutil

import mlflow
import torch
from gliner import GLiNER

from ml_pipeline.lib.gliner_evaluate import compute_scores, format_errors, format_report, prf

# ---------------------------------------------------------------------------
# Réglages : modifiez-les ici, un seul par essai
# ---------------------------------------------------------------------------
CONFIG = {
    "base_model": "urchade/gliner_medium-v2.1",
    "epochs": 50,
    "lr": 5e-6,              # learning rate de l'encodeur
    "others_lr": 1e-5,       # learning rate des couches GLiNER
    "batch_size": 8,
    "seed": 42,              # même seed = même entraînement
    "keep_checkpoints": False,
    "output_dir": "gliner_model",
    "experiment": "gliner_art",
}

parser = argparse.ArgumentParser()
parser.add_argument("--train", required=True)
parser.add_argument("--eval", required=True)
parser.add_argument("--run-name", default=None)
args = parser.parse_args()

# ---------------------------------------------------------------------------
# Données : chargement + vérification avant de lancer quoi que ce soit
# ---------------------------------------------------------------------------
def load_and_check(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for i, ex in enumerate(data):
        n = len(ex["tokenized_text"])
        for s, e, lab in ex["ner"]:
            if not (0 <= s <= e < n):
                raise ValueError(f"{path}, exemple {i} : span [{s}, {e}] hors de la phrase ({n} tokens)")
    return data

train_data = load_and_check(args.train)
eval_data = load_and_check(args.eval)
labels = sorted({lab for ex in train_data + eval_data for _, _, lab in ex["ner"]})

# Un label présent dans eval mais absent de train ne pourra pas être appris
missing = {lab for ex in eval_data for _, _, lab in ex["ner"]} - {lab for ex in train_data for _, _, lab in ex["ner"]}
if missing:
    print(f"⚠ Labels présents en validation mais jamais en entraînement : {sorted(missing)}")

print(f"{len(train_data)} phrases d'entraînement, {len(eval_data)} de validation, {len(labels)} labels")

# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------
device = "cuda" if torch.cuda.is_available() else "cpu"
model = GLiNER.from_pretrained(CONFIG["base_model"]).to(device)

mlflow.set_experiment(CONFIG["experiment"])

with mlflow.start_run(run_name=args.run_name) as run:
    run_dir = f"{CONFIG['output_dir']}/{run.info.run_id}"   # un dossier par run : rien n'est écrasé

    mlflow.log_params({
        **{k: v for k, v in CONFIG.items() if k not in ("output_dir", "experiment")},
        "n_train": len(train_data),
        "n_eval": len(eval_data),
        "labels": ", ".join(labels),
    })
    mlflow.log_artifact(args.train, artifact_path="data")
    mlflow.log_artifact(args.eval, artifact_path="data")

    # -----------------------------------------------------------------------
    # Entraînement
    # -----------------------------------------------------------------------
    model.train_model(
        train_dataset=train_data,
        eval_dataset=eval_data,
        output_dir=run_dir,
        num_train_epochs=CONFIG["epochs"],
        max_steps=-1,                 # c'est le nombre d'époques qui décide de la durée
        learning_rate=CONFIG["lr"],
        others_lr=CONFIG["others_lr"],
        weight_decay=0.01,
        others_weight_decay=0.01,
        lr_scheduler_type="linear",
        warmup_ratio=0.1,
        per_device_train_batch_size=CONFIG["batch_size"],
        per_device_eval_batch_size=CONFIG["batch_size"],
        focal_loss_alpha=0.75,
        focal_loss_gamma=2,
        eval_strategy="epoch",
        save_strategy="epoch",
        logging_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,  # on garde l'époque avec la meilleure eval_loss
        seed=CONFIG["seed"],
        dataloader_num_workers=0,
        use_cpu=(device == "cpu"),
        report_to="mlflow",
    )

    # -----------------------------------------------------------------------
    # Sauvegarde du meilleur modèle, en local et dans MLflow
    # -----------------------------------------------------------------------
    final_dir = f"{run_dir}/final"
    model.save_pretrained(final_dir)
    mlflow.log_artifacts(final_dir, artifact_path="model")

    # Les checkpoints pèsent plusieurs centaines de Mo et ne servent plus
    if not CONFIG["keep_checkpoints"]:
        for ckpt in glob.glob(f"{run_dir}/checkpoint-*"):
            shutil.rmtree(ckpt)

    # -----------------------------------------------------------------------
    # Évaluation : global + par label
    # -----------------------------------------------------------------------
    total, counts, errors = compute_scores(model, eval_data, labels)
    p, r, f1 = total
    mlflow.log_metrics({"eval_precision": p, "eval_recall": r, "eval_f1": f1})
    for lab, (t, f, m) in counts.items():
        if t + m:  # un F1 sur zéro exemple ne veut rien dire
            mlflow.log_metric(f"f1_{lab.replace(' ', '_')}", prf(t, f, m)[2])

    report = format_report(total, counts)
    mlflow.log_text(report, "evaluation.txt")
    mlflow.log_text(format_errors(errors), "erreurs.txt")
    print("\n" + report)

    print(f"\nRun terminé : {run.info.run_id}")
    print(f"Modèle sauvegardé dans : {final_dir}")