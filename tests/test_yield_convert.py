# -*- coding: utf-8 -*-
"""产量单位组合式换算测试"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding="utf-8")

from src.core.models import _convert_yield, _normalize_unit, _parse_density_per_ha, _parse_plot_size_m2

# 默认单位换算表（用于测试）
_MASS_TO_KG = {"g": 0.001, "kg": 1.0, "t": 1000.0, "mg": 0.000001, "Mg": 1000.0, "斤": 0.5, "公斤": 1.0, "万kg": 10000000.0, "ton": 1000.0, "tonne": 1000.0}
_AREA_TO_HA = {"m2": 0.0001, "m²": 0.0001, "平方米": 0.0001, "ha": 1.0, "hm2": 1.0, "hm²": 1.0, "亩": 0.0666667, "acre": 0.404686, "667m2": 0.0666667}
_CONTEXT_PLOT = {"plot", "小区"}
_CONTEXT_PLANT = {"plant", "株", "pot", "盆", "ear", "穗", "hill", "穴", "棵"}


def convert_yield(value, unit, plot_size="", planting_density=""):
    """包装函数，使用默认参数调用 _convert_yield"""
    return _convert_yield(
        value, unit,
        _MASS_TO_KG, _AREA_TO_HA,
        _CONTEXT_PLOT, _CONTEXT_PLANT,
        plot_size=plot_size,
        planting_density=planting_density,
    )


all_ok = True

def check(desc, result, expected):
    global all_ok
    ok = result == expected
    if not ok:
        all_ok = False
    print(f"  {'OK' if ok else 'FAIL'} | {desc}: -> {result} (expect {expected})")

# ── 第 1 层: 标准组合 ──
print("=== Layer 1: Compositional ===")
check("kg/ha", convert_yield(1000, "kg/ha"), 1000.0)
check("t/ha", convert_yield(1.0, "t/ha"), 1000.0)
check("Mg\u00b7ha\u207b\u00b9", convert_yield(1.0, "Mg\u00b7ha\u207b\u00b9"), 1000.0)
check("g/m\u00b2", convert_yield(100, "g/m\u00b2"), 1000.0)
check("kg/\u4ea9", convert_yield(500, "kg/\u4ea9"), 7500.0)
check("\u65a4/\u4ea9", convert_yield(500, "\u65a4/\u4ea9"), 3750.0)
check("kg/667m\u00b2", convert_yield(600, "kg/667m\u00b2"), round(600 / 0.0666667, 2))
check("kg\u00b7hm\u207b\u00b2", convert_yield(1.0, "kg\u00b7hm\u207b\u00b2"), 1.0)
check("t\u00b7hm\u207b\u00b2", convert_yield(1.0, "t\u00b7hm\u207b\u00b2"), 1000.0)
check("g/hm\u00b2", convert_yield(100, "g/hm\u00b2"), 0.1)
check("ton/ha", convert_yield(1.0, "ton/ha"), 1000.0)
check("tonne/ha", convert_yield(1.0, "tonne/ha"), 1000.0)
check("g/ha", convert_yield(100, "g/ha"), 0.1)
check("kg per ha", convert_yield(1.0, "kg per ha"), 1.0)
check("g m\u207b\u00b2", convert_yield(100, "g m\u207b\u00b2"), 1000.0)
check("kg/\u5e73\u65b9\u7c73", convert_yield(1.0, "kg/\u5e73\u65b9\u7c73"), 10000.0)
check("\u4e07kg/ha", convert_yield(1.0, "\u4e07kg/ha"), 10000.0)
check("kg/acre", convert_yield(1.0, "kg/acre"), round(1.0 / 0.404686, 2))

# ── 第 2 层: 上下文辅助 ──
print("\n=== Layer 2: Context-assisted ===")
check("kg/plot + 13.3m\u00b2", convert_yield(10, "kg/plot", plot_size="13.3 m\u00b2"), round(10 * 10000 / 13.3, 2))
check("g/plot + 20m\u00b2", convert_yield(500, "g/plot", plot_size="20 m\u00b2"), round(0.5 * 10000 / 20, 2))
check("g/\u682a + 22.5\u4e07\u7a74/ha", convert_yield(250, "g/\u682a", planting_density="22.5\u4e07\u7a74/\u516c\u9877"), round(0.25 * 225000, 2))
check("kg/plant + 30\u00d712cm", convert_yield(0.5, "kg/plant", planting_density="30\u00d712 cm"), round(0.5 * 10000 / (0.30 * 0.12), 2))
check("g/pot + 225000\u682a/ha", convert_yield(100, "g/pot", planting_density="225000\u682a/ha"), round(0.1 * 225000, 2))
check("kg/plot no ps -> None", convert_yield(10, "kg/plot"), None)
check("g/\u682a no pd -> None", convert_yield(250, "g/\u682a"), None)

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
check("30\u00d712 cm", _parse_density_per_ha("30\u00d712 cm"), round(10000.0 / (0.30 * 0.12), 2))
check("22.5\u4e07\u7a74/\u516c\u9877", _parse_density_per_ha("22.5\u4e07\u7a74/\u516c\u9877"), 225000.0)
check("15000\u682a/\u4ea9", _parse_density_per_ha("15000\u682a/\u4ea9"), 225000.0)
check("225000 plants/ha", _parse_density_per_ha("225000 plants/ha"), 225000.0)
check("\u884c\u8ddd 30cm\u3001\u682a\u8ddd 12cm", _parse_density_per_ha("\u884c\u8ddd 30cm\u3001\u682a\u8ddd 12cm"), round(10000.0 / (0.30 * 0.12), 2))
check("\u682a\u884c\u8ddd 30\u00d712cm", _parse_density_per_ha("\u682a\u884c\u8ddd 30\u00d712cm"), round(10000.0 / (0.30 * 0.12), 2))
check("\u57fa\u672c\u82d7 150 \u4e07\u682a/\u516c\u9877", _parse_density_per_ha("\u57fa\u672c\u82d7 150 \u4e07\u682a/\u516c\u9877"), 1500000.0)

# ── 小区面积解析 ──
print("\n=== Plot size parse ===")
check("13.3 m\u00b2", _parse_plot_size_m2("13.3 m\u00b2"), 13.3)
check("0.002 ha", _parse_plot_size_m2("0.002 ha"), 20.0)
check("\u5c0f\u533a\u9762\u79ef20m2", _parse_plot_size_m2("\u5c0f\u533a\u9762\u79ef20m2"), 20.0)
check("20 \u5e73\u7c73", _parse_plot_size_m2("20 \u5e73\u7c73"), 20.0)
check("20m^2", _parse_plot_size_m2("20m^2"), 20.0)
check("\u957f 5m\u3001\u5bbd 2.66m", _parse_plot_size_m2("\u957f 5m\u3001\u5bbd 2.66m"), round(5 * 2.66, 2))
check("5m\u00d72.66m", _parse_plot_size_m2("5m\u00d72.66m"), round(5 * 2.66, 2))

# ── NPK 换算为 kg/亩 ──
print("\n=== NPK convert to kg/亩 ===")
# 通过 _convert_yield 算出 kg/ha，再除以 15 得到 kg/亩
def convert_npk(value, unit, plot_size="", planting_density=""):
    """模拟 NPK 换算：先转 kg/ha，再转 kg/亩"""
    kg_per_ha = convert_yield(value, unit, plot_size=plot_size, planting_density=planting_density)
    if kg_per_ha is None:
        return None
    return round(kg_per_ha / 15.0, 2)

check("180 kg/ha N → kg/亩", convert_npk(180, "kg/ha"), round(180 / 15, 2))
check("150 kg/hm² P → kg/亩", convert_npk(150, "kg/hm²"), round(150 / 15, 2))
check("120 kg/亩 K → kg/亩", convert_npk(120, "kg/亩"), 120.0)  # 120 kg/亩 → 1800 kg/ha → 120 kg/亩
check("20000 g/株 + 密度", convert_npk(20000, "g/株", planting_density="22.5万穴/公顷"), round(20 * 225000 / 15, 2))
check("90 kg P2O5/ha → kg/亩", convert_npk(90, "kg/ha"), round(90 / 15, 2))
check("无单位 → None", convert_npk(100, ""), None)

print()
print("ALL PASSED" if all_ok else "SOME FAILED")
