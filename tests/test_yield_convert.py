# -*- coding: utf-8 -*-
"""产量单位组合式换算测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")

from src.core.models import _convert_yield, _normalize_unit, _parse_density_per_ha, _parse_plot_size_m2

all_ok = True

def check(desc, result, expected):
    global all_ok
    ok = result == expected
    if not ok:
        all_ok = False
    print(f"  {'OK' if ok else 'FAIL'} | {desc}: -> {result} (expect {expected})")

# ── 第 1 层: 标准组合 ──
print("=== Layer 1: Compositional ===")
check("kg/ha", _convert_yield(1000, "kg/ha"), 1000.0)
check("t/ha", _convert_yield(1.0, "t/ha"), 1000.0)
check("Mg\u00b7ha\u207b\u00b9", _convert_yield(1.0, "Mg\u00b7ha\u207b\u00b9"), 1000.0)
check("g/m\u00b2", _convert_yield(100, "g/m\u00b2"), 1000.0)
check("kg/\u4ea9", _convert_yield(500, "kg/\u4ea9"), 7500.0)
check("\u65a4/\u4ea9", _convert_yield(500, "\u65a4/\u4ea9"), 3750.0)
check("kg/667m\u00b2", _convert_yield(600, "kg/667m\u00b2"), 9000.0)
check("kg\u00b7hm\u207b\u00b2", _convert_yield(1.0, "kg\u00b7hm\u207b\u00b2"), 1.0)
check("t\u00b7hm\u207b\u00b2", _convert_yield(1.0, "t\u00b7hm\u207b\u00b2"), 1000.0)
check("g/hm\u00b2", _convert_yield(100, "g/hm\u00b2"), 0.1)
check("ton/ha", _convert_yield(1.0, "ton/ha"), 1000.0)
check("tonne/ha", _convert_yield(1.0, "tonne/ha"), 1000.0)
check("g/ha", _convert_yield(100, "g/ha"), 0.1)
check("kg per ha", _convert_yield(1.0, "kg per ha"), 1.0)
check("g m\u207b\u00b2", _convert_yield(100, "g m\u207b\u00b2"), 1000.0)
check("kg/\u5e73\u65b9\u7c73", _convert_yield(1.0, "kg/\u5e73\u65b9\u7c73"), 10000.0)
check("\u4e07kg/ha", _convert_yield(1.0, "\u4e07kg/ha"), 10000.0)
check("kg/acre", _convert_yield(1.0, "kg/acre"), round(1.0 / 0.404686, 2))

# ── 第 2 层: 上下文辅助 ──
print("\n=== Layer 2: Context-assisted ===")
check("kg/plot + 13.3m\u00b2", _convert_yield(10, "kg/plot", plot_size="13.3 m\u00b2"), round(10 * 10000 / 13.3, 2))
check("g/plot + 20m\u00b2", _convert_yield(500, "g/plot", plot_size="20 m\u00b2"), round(0.5 * 10000 / 20, 2))
check("g/\u682a + 22.5\u4e07\u7a74/ha", _convert_yield(250, "g/\u682a", planting_density="22.5\u4e07\u7a74/\u516c\u9877"), round(0.25 * 225000, 2))
check("kg/plant + 30\u00d712cm", _convert_yield(0.5, "kg/plant", planting_density="30\u00d712 cm"), round(0.5 * 10000 / (0.30 * 0.12), 2))
check("g/pot + 225000\u682a/ha", _convert_yield(100, "g/pot", planting_density="225000\u682a/ha"), round(0.1 * 225000, 2))
check("kg/plot no ps -> None", _convert_yield(10, "kg/plot"), None)
check("g/\u682a no pd -> None", _convert_yield(250, "g/\u682a"), None)

# ── 归一化 ──
print("\n=== Normalize ===")
check("kg\u00b7ha\u207b\u00b9", _normalize_unit("kg\u00b7ha\u207b\u00b9"), "kg/ha")
check("kg\u00b7hm\u207b\u00b2", _normalize_unit("kg\u00b7hm\u207b\u00b2"), "kg/hm2")
check("g/m\u00b2", _normalize_unit("g/m\u00b2"), "g/m2")
check("Mg\u00b7ha\u207b\u00b9", _normalize_unit("Mg\u00b7ha\u207b\u00b9"), "Mg/ha")
check("kg per ha", _normalize_unit("kg per ha"), "kg/ha")
check("t\u00b7hm\u207b\u00b2", _normalize_unit("t\u00b7hm\u207b\u00b2"), "t/hm2")

# ── 密度解析 ──
print("\n=== Density parse ===")
check("30\u00d712 cm", _parse_density_per_ha("30\u00d712 cm"), 10000.0 / (0.30 * 0.12))
check("22.5\u4e07\u7a74/\u516c\u9877", _parse_density_per_ha("22.5\u4e07\u7a74/\u516c\u9877"), 225000.0)
check("15000\u682a/\u4ea9", _parse_density_per_ha("15000\u682a/\u4ea9"), 225000.0)
check("225000 plants/ha", _parse_density_per_ha("225000 plants/ha"), 225000.0)

# ── 小区面积解析 ──
print("\n=== Plot size parse ===")
check("13.3 m\u00b2", _parse_plot_size_m2("13.3 m\u00b2"), 13.3)
check("0.002 ha", _parse_plot_size_m2("0.002 ha"), 20.0)
check("\u5c0f\u533a\u9762\u79ef20m2", _parse_plot_size_m2("\u5c0f\u533a\u9762\u79ef20m2"), 20.0)

print()
print("ALL PASSED" if all_ok else "SOME FAILED")
