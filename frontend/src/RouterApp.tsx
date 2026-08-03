import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { LandDetailsPage } from './components/LandDetailsPage';
import { AgreementPreview } from './components/AgreementPreview';
import { AgreementsAppShell } from './components/AgreementsAppShell';
import AutofillAgreementPage from './App';

const routerBasename = (import.meta.env.BASE_URL || '/').replace(/\/$/, '');

export default function RouterApp() {
  return (
    <BrowserRouter basename={routerBasename || undefined}>
      <Routes>
        <Route path="/" element={<Navigate to="/agreements" replace />} />
        <Route element={<AgreementsAppShell />}>
          <Route path="/agreements" element={<LandDetailsPage />} />
          <Route path="/agreements/new" element={<AutofillAgreementPage />} />
          <Route path="/agreements/:id/edit" element={<AutofillAgreementPage />} />
          <Route path="/agreements/:id" element={<AgreementPreview />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
