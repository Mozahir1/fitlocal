# proportion.py ───────────────────────────────────────────────────────────────
import sys
from pathlib import Path
from typing import Optional, Dict, Tuple

import mediapipe as mp
import numpy  as np
from PIL import Image
import cv2                                # new
import torch
from torchvision import transforms

mp_pose = mp.solutions.pose
device  = "cuda" if torch.cuda.is_available() else "cpu"

class ProportionScorer:
    def __init__(self):
        self.pose = mp_pose.Pose(static_image_mode=True)

    # ───────────────── LANDMARKS ─────────────────
    def extract_landmarks(self, img: Image.Image) -> Optional[Dict[str, np.ndarray]]:
        img_np = np.array(img.convert("RGB"))
        res    = self.pose.process(img_np)
        if not res.pose_landmarks:
            return None
        lm = res.pose_landmarks.landmark
        kp = lambda i: np.array([lm[i].x, lm[i].y])
        return {
            "l_sh": kp(11), "r_sh": kp(12),
            "l_hp": kp(23), "r_hp": kp(24),
            "l_kn": kp(25), "r_kn": kp(26),
            "l_an": kp(27), "r_an": kp(28),
            "l_el": kp(13), "r_el": kp(14),
            "l_wr": kp(15), "r_wr": kp(16),
        }

    # ───────────────── BODY METRICS ──────────────
    @staticmethod
    def _d(a, b): return np.linalg.norm(a - b)

    def compute_body_metrics(self, L: Dict[str, np.ndarray]) -> Dict[str, float]:
        torso = (self._d(L["l_sh"], L["l_hp"]) + self._d(L["r_sh"], L["r_hp"])) / 2
        leg   = (self._d(L["l_hp"], L["l_kn"]) + self._d(L["l_kn"], L["l_an"]) +
                 self._d(L["r_hp"], L["r_kn"]) + self._d(L["r_kn"], L["r_an"])) / 2
        arm_sym = abs((self._d(L["l_sh"], L["l_el"]) + self._d(L["l_el"], L["l_wr"])) -
                      (self._d(L["r_sh"], L["r_el"]) + self._d(L["r_el"], L["r_wr"])))
        leg_sym = abs((self._d(L["l_hp"], L["l_kn"]) + self._d(L["l_kn"], L["l_an"])) -
                      (self._d(L["r_hp"], L["r_kn"]) + self._d(L["r_kn"], L["r_an"])))
        return {
            "torso_leg_ratio":   torso / (leg + 1e-6),
            "arm_symmetry_diff": arm_sym,
            "leg_symmetry_diff": leg_sym,
        }

    # ───────────────── CLOTHING BREAKPOINTS ──────
    def detect_clothing_breakpoints(
        self, img_np: np.ndarray, L: Dict[str, np.ndarray]
    ) -> Dict[str, int]:
        h, w, _ = img_np.shape
        gray   = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        sobel  = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        abs_sb = np.abs(sobel)

        def strongest_row(guess_y, win=25):
            y0 = max(0, int(guess_y - win))
            y1 = min(h - 1, int(guess_y + win))
            band = abs_sb[y0:y1]
            local = np.argmax(band.mean(axis=1))
            return y0 + local

        # landmark y‑coords in pixels
        y_sh = int(((L["l_sh"][1] + L["r_sh"][1]) / 2) * h)
        y_hp = int(((L["l_hp"][1] + L["r_hp"][1]) / 2) * h)
        y_kn = int(((L["l_kn"][1] + L["r_kn"][1]) / 2) * h)
        y_wr = int(((L["l_wr"][1] + L["r_wr"][1]) / 2) * h)

        waistband_y   = strongest_row(y_hp)
        jacket_end_y  = strongest_row(int(y_hp + 0.5 * (y_kn - y_hp)))
        sleeve_y_l    = strongest_row(y_wr)
        # basic hem symmetry diff (height difference left vs right)
        hem_diff_px   = abs( waistband_y - jacket_end_y )

        return {
            "waistband_y":  waistband_y,
            "jacket_end_y": jacket_end_y,
            "sleeve_y":     sleeve_y_l,
            "hem_sym_px":   hem_diff_px,
            "img_h":        h
        }

    # ───────────────── VISIBLE RATIOS ────────────
    def compute_visible_ratios(
        self, breaks: Dict[str, int], L: Dict[str, np.ndarray]
    ) -> Dict[str, float]:
        h = breaks["img_h"]
        y_sh = ((L["l_sh"][1] + L["r_sh"][1]) / 2) * h
        y_an = ((L["l_an"][1] + L["r_an"][1]) / 2) * h

        vis_torso = breaks["waistband_y"] - y_sh
        vis_leg   = y_an - breaks["waistband_y"]
        leg_torso = vis_leg / (vis_torso + 1e-6)

        sleeve_sym = breaks["sleeve_y"] / h   # simple normalized sleeve endpoint
        hem_sym    = breaks["hem_sym_px"] / h

        return {
            "vis_leg_torso_ratio": leg_torso,
            "sleeve_sym": sleeve_sym,
            "hem_sym": hem_sym,
        }

    # ───────────────── BALANCE SCORE ─────────────
    def outfit_balance_score(self, m: Dict[str, float], v: Dict[str, float]) -> int:
        score = 50
        if m["torso_leg_ratio"] < 0.9 and v["vis_leg_torso_ratio"] < m["torso_leg_ratio"]*0.9:
            score += 20
        if m["torso_leg_ratio"] > 1.1 and v["vis_leg_torso_ratio"] > m["torso_leg_ratio"]*1.1:
            score += 20
        if m["arm_symmetry_diff"] > 0.08 and v["sleeve_sym"] < 0.05:
            score +=10
        if m["leg_symmetry_diff"] > 0.08 and v["hem_sym"] < 0.05:
            score +=10
        return min(score, 100)

# ──────────────────────────────────────────────────────────────────────────────
def _analyze_image(img_path: Path) -> None:
    scorer = ProportionScorer()
    img    = Image.open(img_path)
    lm     = scorer.extract_landmarks(img)
    if lm is None:
        print(f"[{img_path.name}]  ❌  Could not detect a full body.")
        return

    body    = scorer.compute_body_metrics(lm)
    brk     = scorer.detect_clothing_breakpoints(np.array(img.convert("RGB")), lm)
    vis_rat = scorer.compute_visible_ratios(brk, lm)
    balance = scorer.outfit_balance_score(body, vis_rat)

    # ── pretty print ───────────────────────────────────────────
    print(f"\n[{img_path.name}]")
    print("-"*(len(img_path.name)+2))
    print(f" Torso ÷ Leg ratio  : {body['torso_leg_ratio']:.3f}")
    print(f" Arm symmetry diff  : {body['arm_symmetry_diff']:.3f}")
    print(f" Leg symmetry diff  : {body['leg_symmetry_diff']:.3f}")
    print(f" Visible leg÷torso  : {vis_rat['vis_leg_torso_ratio']:.2f}")
    print(f" Sleeve symmetry    : {vis_rat['sleeve_sym']:.2f}")
    print(f" Hem   symmetry     : {vis_rat['hem_sym']:.2f}")
    verdict = "✅ Balanced" if balance>=75 else "⚠️  Could improve" if balance>=50 else "🧦 Off‑balance"
    print(f" ► Outfit‑balance   : {balance} / 100   {verdict}")

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python proportion.py <image1> [image2 …]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.is_file():
            print(f"File not found: {p}")
            continue
        _analyze_image(p)
