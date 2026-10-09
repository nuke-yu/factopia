import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { Line, OrbitControls } from "@react-three/drei";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import * as THREE from "three";
import type { Market, Overview } from "../../api";
import { cssVar, MARKET_CITY, reducedMotion, useThemeVersion } from "../../lib";

const R = 2;
// 伦敦与巴黎很近：伦敦标签放左上（右对齐），巴黎放右下，避免重叠
const LABEL_OFFSET: Record<Market, [number, number, number]> = { hk: [0.2, 0.1, 0], uk: [-0.18, 0.26, 0], fr: [0.2, -0.24, 0] };

/** 用画布纹理绘制的文字标签（始终面向镜头），不依赖 DOM 叠加层。 */
function Label({ lines, align, position }: { lines: { text: string; color: string; size: number; font: string }[]; align: "left" | "right"; position: [number, number, number] }) {
  const [fontsReady, setFontsReady] = useState(0);
  useEffect(() => { document.fonts?.ready.then(() => setFontsReady(1)); }, []);
  const { texture, aspect } = useMemo(() => {
    const scale = 2, w = 360 * scale, h = 64 * scale;
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext("2d")!;
    ctx.textAlign = align; ctx.textBaseline = "top";
    let y = 4 * scale;
    for (const l of lines) {
      ctx.font = `${l.size * scale}px ${l.font}`;
      ctx.fillStyle = l.color;
      ctx.fillText(l.text, align === "left" ? 2 : w - 2, y);
      y += (l.size + 6) * scale;
    }
    const t = new THREE.CanvasTexture(canvas);
    t.colorSpace = THREE.SRGBColorSpace;
    return { texture: t, aspect: w / h };
  }, [lines, align, fontsReady]); // eslint-disable-line react-hooks/exhaustive-deps
  const height = 0.5;
  return (
    <sprite position={position} scale={[height * aspect, height, 1]} center={new THREE.Vector2(align === "left" ? 0 : 1, 0.5)}>
      <spriteMaterial map={texture} transparent depthTest={false} toneMapped={false} />
    </sprite>
  );
}
const toXYZ = (lat: number, lon: number, r = R) => {
  const phi = ((90 - lat) * Math.PI) / 180, theta = ((lon + 180) * Math.PI) / 180;
  return new THREE.Vector3(-r * Math.sin(phi) * Math.cos(theta), r * Math.cos(phi), r * Math.sin(phi) * Math.sin(theta));
};

/** 两点之间抬升的弧线（大圆插值后按高度抬起）。 */
function arc(a: THREE.Vector3, b: THREE.Vector3, lift: number): THREE.Vector3[] {
  const pts: THREE.Vector3[] = [];
  for (let i = 0; i <= 64; i++) {
    const t = i / 64;
    const p = a.clone().normalize().lerp(b.clone().normalize(), t).normalize();
    pts.push(p.multiplyScalar(R + Math.sin(Math.PI * t) * lift));
  }
  return pts;
}

function Traveller({ path, color, speed, offset }: { path: THREE.Vector3[]; color: string; speed: number; offset: number }) {
  const ref = useRef<THREE.Mesh>(null);
  useFrame(({ clock }) => {
    if (!ref.current) return;
    const t = (clock.elapsedTime * speed + offset) % 1;
    ref.current.position.copy(path[Math.floor(t * (path.length - 1))]);
  });
  return (
    <mesh ref={ref}>
      <sphereGeometry args={[0.035, 8, 8]} />
      <meshBasicMaterial color={color} toneMapped={false} />
    </mesh>
  );
}

function Scene({ overview, colors }: { overview: Overview; colors: Record<string, string> }) {
  const spin = useRef<THREE.Group>(null);
  const markets = Object.keys(overview.markets) as Market[];
  const pos = useMemo(() => Object.fromEntries(markets.map((m) => [m, toXYZ(overview.markets[m].lat, overview.markets[m].lon)])) as Record<Market, THREE.Vector3>, [overview]); // eslint-disable-line react-hooks/exhaustive-deps
  const arcs = useMemo(() => [
    { from: "hk" as Market, to: "uk" as Market, lift: 1.0 },
    { from: "uk" as Market, to: "fr" as Market, lift: 0.18 },
    { from: "fr" as Market, to: "hk" as Market, lift: 0.9 },
  ].map((a) => ({ ...a, pts: arc(pos[a.from], pos[a.to], a.lift) })), [pos]);
  // 经纬线点阵
  const dots = useMemo(() => {
    const arr: number[] = [];
    for (let lat = -80; lat <= 80; lat += 5) for (let lon = -180; lon < 180; lon += 5) {
      const v = toXYZ(lat, lon, R * 1.001); arr.push(v.x, v.y, v.z);
    }
    return new Float32Array(arr);
  }, []);
  // 让香港与伦敦之间的经度、三地平均纬度朝向镜头，再左右轻微摆动（不整圈旋转，三地始终可见）
  const lons = markets.map((m) => overview.markets[m].lon), lats = markets.map((m) => overview.markets[m].lat);
  const lonC = (Math.max(...lons) + Math.min(...lons)) / 2, latC = lats.reduce((a, b) => a + b, 0) / lats.length;
  useFrame(({ clock }) => {
    if (spin.current) spin.current.rotation.y = reducedMotion() ? 0 : Math.sin(clock.elapsedTime * 0.25) * 0.3;
  });

  return (
    <group ref={spin}>
    <group rotation={[(latC * Math.PI) / 180, ((-90 - lonC) * Math.PI) / 180, 0]}>
      <mesh>
        <sphereGeometry args={[R * 0.995, 64, 64]} />
        <meshStandardMaterial color={colors.bg3} transparent opacity={0.9} />
      </mesh>
      <points>
        <bufferGeometry><bufferAttribute attach="attributes-position" args={[dots, 3]} /></bufferGeometry>
        <pointsMaterial size={0.012} color={colors.line} transparent opacity={0.9} />
      </points>
      {arcs.map((a, i) => (
        <group key={i}>
          <Line points={a.pts} color={colors.accent} lineWidth={1.2} transparent opacity={0.6} />
          <Traveller path={a.pts} color={colors.accent} speed={0.18} offset={i * 0.33} />
        </group>
      ))}
      {markets.map((m) => (
        <group key={m} position={pos[m]}>
          <mesh>
            <sphereGeometry args={[0.07, 16, 16]} />
            <meshBasicMaterial color={colors[m]} toneMapped={false} />
          </mesh>
          <mesh rotation={[Math.PI / 2, 0, 0]}>
            <ringGeometry args={[0.11, 0.14, 32]} />
            <meshBasicMaterial color={colors[m]} side={THREE.DoubleSide} transparent opacity={0.6} toneMapped={false} />
          </mesh>
          <Label position={LABEL_OFFSET[m]} align={m === "uk" ? "right" : "left"} lines={[
            { text: MARKET_CITY[m], color: colors[m], size: 20, font: "'Chakra Petch', sans-serif" },
            { text: `${overview.markets[m].stocks} 只 · ${overview.markets[m].index}`, color: colors.muted, size: 15, font: "'JetBrains Mono', 'PingFang SC', sans-serif" },
          ]} />
        </group>
      ))}
    </group>
    </group>
  );
}

export default function Globe({ overview }: { overview: Overview }) {
  const theme = useThemeVersion();
  const colors = useMemo(() => ({
    accent: cssVar("--accent"), muted: cssVar("--muted"), line: cssVar("--line-2"), bg: cssVar("--bg-2"), bg3: cssVar("--bg-3"),
    hk: cssVar("--hk"), uk: cssVar("--uk"), fr: cssVar("--fr"),
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [theme]);
  return (
    <Canvas camera={{ position: [0, 0, 6.2], fov: 40 }} dpr={[1, 2]}>
      <color attach="background" args={[colors.bg]} />
      <ambientLight intensity={0.6} />
      <directionalLight position={[5, 3, 5]} intensity={1.2} />
      <Scene overview={overview} colors={colors} />
      <OrbitControls enablePan={false} enableZoom={false} />
      <EffectComposer>
        <Bloom intensity={0.8} luminanceThreshold={0.25} mipmapBlur />
      </EffectComposer>
    </Canvas>
  );
}
