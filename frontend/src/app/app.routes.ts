import { Routes } from '@angular/router';
import { LoginComponent } from './login.component';
import { ImageListComponent } from './image-list.component';
import { JobNewComponent } from './job-new.component';
import { JobDetailComponent } from './job-detail.component';
import { MatrixAdminComponent } from './matrix-admin.component';
import { ResultsComponent } from './results.component';
import { authGuard } from './auth.guard';

export const routes: Routes = [
  { path: '', redirectTo: 'images', pathMatch: 'full' },
  { path: 'login', component: LoginComponent },
  { path: 'images', component: ImageListComponent, canActivate: [authGuard] },
  { path: 'jobs/new/:imageId', component: JobNewComponent, canActivate: [authGuard] },
  { path: 'jobs/:id', component: JobDetailComponent, canActivate: [authGuard] },
  { path: 'matrices', component: MatrixAdminComponent, canActivate: [authGuard] },
  { path: 'results', component: ResultsComponent, canActivate: [authGuard] },
  { path: '**', redirectTo: 'images' },
];
