# Synthetic Analog Packaging Knowhow

This synthetic source is safe demo material for `silicon-notebook`. It is not copied from proprietary semiconductor documentation.

## Low-noise input pin assignment

Sensitive analog inputs should not share an immediate bondwire neighborhood with high-current switching returns. For low-noise AFE designs, place quiet ground references close to the input pins and review package parasitics before pinout freeze.

## ESD and quiet pins

ESD clamps and quiet analog pins need a shared package-level review because the return inductance is package dependent. A schematic-clean ESD path can still be noisy at package level.

## Debug case

The lab noise issue disappeared after the input pins were moved away from the switching return cluster. The project added package parasitic extraction to the signoff checklist.

## Checklist

- Are sensitive analog pins separated from high di/dt loops?
- Is the local ground return quiet and continuous?
- Were bondwire parasitics included in simulation?
- Has the ESD discharge path been reviewed with the package owner?

