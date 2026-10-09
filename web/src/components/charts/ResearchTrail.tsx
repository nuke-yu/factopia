import { useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Html, Line, OrbitControls } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import * as THREE from "three";
import type { Round, Run } from "../../api";
import { cssVar, pct, reducedMotion, useThemeVersion } from "../../lib";

type Node = { key: string; label: string; pos: THREE.Vector3; ret: number | null; kind: "baseline" | "adopted" | "rejected" | "running"; round?: Round };

const RADIUS = 3.2, STEP = 1.35, TURN = 1.15;

function layout(run: Run, rounds: Round[]): Node[] {
  const at = (i: number) => new THREE.Vector3(Math.cos(i * TURN) * RADIUS, i * STEP - 3, Math.sin(i * TURN) * RADIUS);
  const nodes: Node[] = [{
    key: "baseline", label: "起点", pos: at(0), ret: run.baseline?.metrics?.annual_return_with_cost ?? null, kind: "baseline",
  }];
  rounds.forEach((r, i) => nodes.push({
    key: String(r.loop), label: `第 ${r.loop} 轮`, pos: at(i + 1), round: r,
    ret: r.metrics?.annual_return_with_cost ?? null,
    kind: r.status !== "done" ? "running" : r.decision ? "adopted" : "rejected",
  }));
  return nodes;
}

function Pillar({ node, colors, selected, onSelect }: {
  node: Node; colors: Record<string, string>; selected: boolean; onSelect: (k: string) => void;
}) {
  const core = useRef<THREE.Mesh>(null);
  const [hover, setHover] = useState(false);
  const h = Math.max(0.08, Math.abs(node.ret ?? 0) * 12); // 柱高：扣成本后年化超额收益
  const color = node.kind === "baseline" ? colors.muted : node.kind === "adopted" ? colors.good : node.kind === "running" ? colors.accent : colors.bad;
  const barColor = (node.ret ?? 0) >= 0 ? colors.accent : colors.bad;
  useFrame(({ clock }) => {
    if (!core.current) return;
    const pulse = node.kind === "running" || selected ? 1 + Math.sin(clock.elapsedTime * 3) * 0.15 : 1;
    core.current.scale.setScalar(pulse * (hover ? 1.25 : 1));
  });
  return (
    <group position={node.pos}>
      <mesh ref={core} onClick={(e) => { e.stopPropagation(); onSelect(node.key); }}
        onPointerOver={(e) => { e.stopPropagation(); setHover(true); document.body.style.cursor = "pointer"; }}
        onPointerOut={() => { setHover(false); document.body.style.cursor = ""; }}>
        <icosahedronGeometry args={[node.kind === "adopted" ? 0.42 : 0.32, 1]} />
        <meshStandardMaterial color={color} emissive={color} emissiveIntensity={selected ? 2.4 : 1.3} toneMapped={false} />
      </mesh>
      {node.ret != null && (
        <mesh position={[0, ((node.ret >= 0 ? 1 : -1) * h) / 2, 0]}>
          <cylinderGeometry args={[0.05, 0.05, h, 8]} />
          <meshBasicMaterial color={barColor} transparent opacity={0.75} toneMapped={false} />
        </mesh>
      )}
      <Html position={[0.55, 0.15, 0]} distanceFactor={11} zIndexRange={[10, 0]} style={{ pointerEvents: "none" }}>
        <div className="whitespace-nowrap text-[11px] leading-tight select-none" style={{ color: selected ? colors.text : colors.muted }}>
          <div className="font-display tracking-wider">{node.label}</div>
          <div className="num" style={{ color }}>{pct(node.ret)}</div>
        </div>
      </Html>
    </group>
  );
}

export default function ResearchTrail({ run, rounds, selected, onSelect }: {
  run: Run; rounds: Round[]; selected: string; onSelect: (k: string) => void;
}) {
  const theme = useThemeVersion();
  const colors = useMemo(() => ({
    accent: cssVar("--accent"), good: cssVar("--good"), bad: cssVar("--bad"), muted: cssVar("--muted"),
    text: cssVar("--text"), line: cssVar("--line-2"), bg: cssVar("--bg-2"),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [theme]);
  const nodes = useMemo(() => layout(run, rounds), [run, rounds]);
  // 每轮都与"当时的最好结果"比较：从被采纳的节点向后续节点画虚线
  const sotaLinks = useMemo(() => {
    const links: [THREE.Vector3, THREE.Vector3][] = [];
    let sota = nodes[0];
    nodes.slice(1).forEach((n) => { links.push([sota.pos, n.pos]); if (n.kind === "adopted") sota = n; });
    return links;
  }, [nodes]);
  const helix = useMemo(() => {
    const pts: THREE.Vector3[] = [];
    const n = nodes.length - 1;
    for (let t = 0; t <= n; t += 0.05) pts.push(new THREE.Vector3(Math.cos(t * TURN) * RADIUS, t * STEP - 3, Math.sin(t * TURN) * RADIUS));
    return pts;
  }, [nodes]);
  const centerY = ((nodes.length - 1) * STEP) / 2 - 3;

  return (
    <Canvas camera={{ position: [9, centerY + 3, 9], fov: 45 }} dpr={[1, 2]} onPointerMissed={() => undefined}>
      <color attach="background" args={[colors.bg]} />
      <ambientLight intensity={0.5} />
      <pointLight position={[6, 10, 6]} intensity={60} />
      <gridHelper args={[16, 16, colors.line, colors.line]} position={[0, -4.2, 0]} />
      <Line points={helix} color={colors.accent} lineWidth={1.5} transparent opacity={0.55} />
      {sotaLinks.map(([a, b], i) => (
        <Line key={i} points={[a, b]} color={colors.muted} lineWidth={0.8} dashed dashSize={0.18} gapSize={0.14} transparent opacity={0.5} />
      ))}
      {nodes.map((n) => <Pillar key={n.key} node={n} colors={colors} selected={selected === n.key} onSelect={onSelect} />)}
      <OrbitControls target={[0, centerY, 0]} enablePan={false} autoRotate={!reducedMotion()} autoRotateSpeed={0.6} minDistance={6} maxDistance={24} />
      <EffectComposer>
        <Bloom intensity={0.9} luminanceThreshold={0.2} mipmapBlur />
      </EffectComposer>
    </Canvas>
  );
}

