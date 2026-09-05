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
import { Diagnosis } from "./pages/Diagnosis";
import { Tools } from "./pages/Tools";
import { useStore } from "./store/useStore";

export default function App() {
  const boot = useStore((s) => s.boot);
  useEffect(() => {
    boot();
  }, [boot]);

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="/g/:id" element={<GeneratorView />} />
        <Route path="/tools" element={<Tools />} />
        <Route path="/gallery" element={<Gallery />} />
        <Route path="/trash" element={<Trash />} />
        <Route path="/queue" element={<Queue />} />
        <Route path="/grid/:groupId" element={<GridView />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/diagnosis" element={<Diagnosis />} />
        {/* Without this an unknown path rendered the chrome around a blank
            area, which reads as a broken page rather than a wrong URL. */}
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
