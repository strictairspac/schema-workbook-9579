import {migrationApi} from '../api/migration.js';
export const migrationStore={state:null,members:[],queried:false,async refresh(){this.state=await migrationApi.status();return this.state;},async lookup(attribute){this.queried=true;this.members=await migrationApi.members(attribute);return this.members;}};
