import { useEffect } from "react";
import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { GeneratorView } from "./pages/GeneratorView";
import { Gallery } from "./pages/Gallery";
import { NotFound } from "./pages/NotFound";
import { Trash } from "./pages/Trash";
import { GridView } from "./pages/GridView";
import { Queue } from "./pages/Queue";
import { Settings } from "./pages/Settings";
import { Resources } from "./pages/Resources";
import { ReferenceSets } from "./pages/ReferenceSets";
import { Canvas } from "./pages/Canvas";
import { Diagnosis } from "./pages/Diagnosis";
import { Tools } from "./pages/Tools";
import { VariantSetCreate } from "./pages/VariantSetCreate";
import { VariantSetDetail } from "./pages/VariantSetDetail";
import { Recipes } from "./pages/Recipes";
import { PresetLibrary } from "./pages/PresetLibrary";
import { Studio } from "./pages/Studio";
import { VariantSets } from "./pages/VariantSets";
import { useStore } from "./store/useStore";

export default function App() {
  const boot = useStore((s) => s.boot);
  useEffect(() => {
    boot();
  }, [boot]);

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Studio />} />
        {/* Every generator and tool, including video: one click from the Studio. */}
        <Route path="/processes" element={<Dashboard />} />
        <Route path="/g/:id" element={<GeneratorView />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/gallery" element={<Gallery />} />
        <Route path="/trash" element={<Trash />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/grid/:groupId" element={<GridView />} />
        <Route path="/recipes" element={<Recipes />} />
        <Route path="/recipes/:key" element={<Recipes />} />
        {/* Preset ids carry a "/" ("custom/my-look"), so the rest of the path is the id. */}
        <Route path="/presets/*" element={<PresetLibrary />} />
        <Route path="/variants" element={<VariantSets />} />
        <Route path="/variants/new" element={<VariantSetCreate />} />
        <Route path="/variants/:id" element={<VariantSetDetail />} />
        <Route path="/resources" element={<Resources />} />
        <Route path="/references" element={<ReferenceSets />} />
        <Route path="/references/:id" element={<ReferenceSets />} />
        <Route path="/canvas" element={<Canvas />} />
        <Route path="/canvas/:id" element={<Canvas />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/diagnosis" element={<Diagnosis />} />
        {/* Without this an unknown path rendered the chrome around a blank
            area, which reads as a broken page rather than a wrong URL. */}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
