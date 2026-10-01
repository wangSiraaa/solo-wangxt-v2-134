import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { ApiService } from './api.service';

export const authGuard: CanActivateFn = () => {
  const api = inject(ApiService);
  if (api.apiKey()) return true;
  return inject(Router).createUrlTree(['/login']);
};
