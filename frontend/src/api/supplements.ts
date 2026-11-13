import {request} from './client';import type {SupplementBatch,VersionTrail} from '../types/entities';
export const list=(datasetId?:string)=>request<SupplementBatch[]>('/supplements'+(datasetId?`?dataset_id=${encodeURIComponent(datasetId)}`:''));
export const detail=(id:string)=>request<SupplementBatch>('/supplements/'+id);
export const create=(data:unknown)=>request<SupplementBatch>('/supplements','POST',data);
export const versionTrail=(datasetId:string)=>request<VersionTrail>(`/datasets/${encodeURIComponent(datasetId)}/version-trail`);
